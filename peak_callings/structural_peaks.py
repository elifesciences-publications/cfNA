
#!/usr/bin/env python

import sys
import os
import re
import pysam
import subprocess
import pandas as pd
import numpy as np
from functools import lru_cache, partial
from multiprocessing import Pool, Manager
from pybedtools import BedTool, set_tempdir
import random
import RNA
from operator import itemgetter
from sequencing_tools.stats_tools import p_adjust
from sequencing_tools.fastq_tools import reverse_complement
from concensus_seq import concensus
from exon_coverage import ExonFilter
import pyBigWig as pbw
from tblout_parser import read_tbl
from sequencing_tools.bam_tools import get_strand
import pyximport
pyximport.install()
from junction_function import get_junction


set_tempdir('/stor/scratch/Lambowitz/cdw2854')


class GenicIntersect():
    def __init__(self):
        self.exons = '/stor/work/Lambowitz/ref/hg19_ref/genes/exons.gencode.bed.gz'
        self.introns = '/stor/work/Lambowitz/ref/hg19_ref/genes/introns.gencode.bed.gz'
        self.tabix = pysam.Tabixfile(self.introns)
        bam = '/stor/work/Lambowitz/cdw2854/cfNA/tgirt_map/merged_bam/dedup/unfragmented.chrM_filter.dedup.bam'
        self.bam = pysam.Samfile(bam)
        self.pseudogene = '/stor/work/Lambowitz/ref/hg19_ref/genes/small_pseudogenes.bed.gz'
        
    def compute_psi(self, chrom, peak_start, peak_end, strand):
        aln_count = 0
        spliced_count = 0
        peak_start, peak_end = int(peak_start), int(peak_end)
        peak_size = peak_end - peak_start
        overlapped_threshold = peak_size * 0.1
        for aln in self.bam.fetch(chrom, peak_start, peak_end):
            if (aln.is_read1 and get_strand(aln) == strand)  or \
                    (aln.is_read2 and get_strand(aln) != strand):
                aln_count += 1
                if 'S' in aln.cigarstring and \
                        self.__test_splice__(aln, peak_start, peak_end, overlapped_threshold):
                    spliced_count+=1
                    
        psi = spliced_count/aln_count if aln_count!=0 else 0
        return psi
    
    def __test_splice__(self, spliced_aln, peak_start, peak_end, th):
        introns = get_junction(spliced_aln)
        coordinates = (intron.split(':')[1].split('_')[0].split('-') for intron in introns)
        overlapped = ( min(peak_end, int(coor[1])) - max(peak_start, int(coor[0])) for coor in coordinates)
        return any(overlap > th  for overlap in overlapped)
        
    
    def intersect(self, bed):
        needed_columns = ['chrom','start','end','peakname', 'pileup','strand']
        self.columns = needed_columns + list(set(bed.columns.tolist()) - set(needed_columns))
        out_columns = self.columns + ['filenum','gchrom','gstart','gend',
                                'gname','gscore','gstrand','gtype','overlap']
        self.inbed = bed\
            .filter(self.columns)
        b_files = [self.exons, self.introns, self.pseudogene] 
        return self.inbed \
            .pipe(BedTool().from_dataframe)\
            .intersect(b = b_files,
                      wao = True, s=True, f=0.2)\
            .to_dataframe(names = out_columns)
            
    def check_intron(self, pchrom, ps, pe, gs, ge):
        rt = 'Exon'
        for intron in self.tabix.fetch(pchrom, ps, pe):
            fields = intron.strip().split('\t')
            istart, iend = int(fields[1]), int(fields[2]) 
            right_continous_exon_intron = (istart - 3) < ge < (istart +3) 
            left_continous_exon_intron = (iend - 3) < gs < (iend + 3)
            right_not_peak_included = iend > pe
            left_not_peak_included = istart < ps
            overlapped = min(pe, iend) - max(ps, istart)
            if (right_continous_exon_intron and right_not_peak_included) \
                    or (left_continous_exon_intron and  left_not_peak_included):
                if overlapped > 10:
                    rt = 'Exon-intron'
        return rt               


    def categorize(self, row):
        gt = row['gtype']
        if row['gtype'] == 'intron':
            if row['overlap_score'] > 0.8:
                gt = 'Full-length intron'
            elif row['psi'] < 0.1:
                gt = 'Within intron'
            elif row['psi'] > 0.2:
                gt =self.check_intron(row['chrom'],
                        row['start'], row['end'],
                        row['gstart'], row['gend'])    
        elif 'pseudo' in row['gtype']:
            gt = 'Pseudogene'
        elif row['gtype'] == 'exon':
            gt =self.check_intron(row['chrom'],
                        row['start'], row['end'],
                        row['gstart'], row['gend'])

        return gt


    def resolve_genic(self, df):
        gbed = df \
            .assign(peak_size = lambda d: d.end - d.start)\
            .assign(gsize = lambda d: d.gend - d.gstart)\
            .set_index('peakname')\
            .pipe(dd.from_pandas, npartitions=24)\
            .groupby(self.columns)\
            .apply(select_annotation, meta = {'gstart':'f8',
                                            'gend':'f8',
                                            'overlap_score':'f8',
                                            'gtype':'f8',
                                            'goverlap':'f8',
                                            'peak_overlap':'f8'})\
            .compute(scheduler='processes')
        gbed.index = gbed.index.droplevel(-1)
        gbed = gbed.reset_index()\
            .assign(gtype = lambda d: [self.categorize(row) for i, row in d.iterrows()])   \
            .assign(gtype = lambda d: np.where((d.gtype=='Exon-intron') & (d.peak_overlap.astype(float) > 0.6),
                                            'Exon',
                                            d.gtype))
        return gbed


 
def select_annotation(df):
    return df \
        .assign(peak_overlap = lambda d: d.overlap / (d.peak_size))\
        .assign(goverlap = lambda d: d.overlap / (d.gsize))\
        .assign(overlap_score = lambda d:  d.peak_overlap * d.goverlap )\
        .nlargest(1,'overlap_score')  \
        .filter(['gstart','gend','overlap_score','gtype','goverlap','peak_overlap']) 



class mRNAFilter():
    '''
    if the peak is on exon?
    is the peak also called in transcriptome?
    '''
    def __init__(self):
        ref_path = '/stor/work/Lambowitz/ref/hg19_ref/genes'
        exons = ref_path + '/gencode.exon.bed.gz'
        self.exons = pysam.Tabixfile(exons)
        transcriptom_peaks = '/stor/work/Lambowitz/cdw2854/cfNA/tgirt_map/transcriptome/macs2/unfragmented.fwd_peaks_genomics.narrowPeak.gz'
        self.transcriptome_peaks = pysam.Tabixfile(transcriptom_peaks)

    def search(self, chrom, start, end, attribute = 'exon'):
        if attribute == 'exon':
            it = self.exons
        elif attribute == 'transcriptome':
            it = self.transcriptome_peaks
        return 'yes' if any(True for frag in it.fetch(chrom, start, end) if  self.__overlap_test__(start, end, frag)) else 'no'

    def __overlap_test__(self, start, end, fragment):
        fields = fragment.split('\t')
        return int(fields[1]) < start and int(fields[2]) > end


class WPS:
    '''
    Window protection scores
    '''
    def __init__(self, start, end, window = 20):
        self.start = int(start)
        self.end = int(end)
        self.window = window
        self.half_window = int(self.window/2)
        self.range = self.end - self.start + self.window
        self.WPS_array = np.zeros(self.range)

    def add_fragment(self, frag_start, frag_end):
        frag_start, frag_end = int(frag_start), int(frag_end)
        frag_size = frag_end - frag_start
        self.fragment_holder = np.zeros(frag_size + self.window)
        self.fragment_holder[self.half_window:-self.half_window] = 1
        self.fragment_holder[self.fragment_holder != 1] = -1
        offset = self.start - (frag_start - self.half_window)  # how far is the aln start relative to peak start
        if offset > 0:
            wps = self.fragment_holder[offset:]
            end = len(wps) if len(wps) < self.range else self.range
            self.WPS_array[:end] += wps[:end]
        else:
            baseShifted = abs(offset)
            end = self.range if baseShifted + len(self.fragment_holder) > self.range else baseShifted + len(self.fragment_holder)
            alignedBases = self.range + offset
            wps = self.fragment_holder[:alignedBases]
            self.WPS_array[baseShifted:end] += wps 
        



class PeakAnalyzer:
    def __init__(self,
            genome = '/stor/work/Lambowitz/ref/hg19_ref/genome/hg19_genome.fa',
            RNAshapes = '/stor/work/Lambowitz/cdw2854/src/miniconda3/bin/RNAshapes',
            gene_bed = '/stor/work/Lambowitz/ref/hg19_ref/genes/genes.bed.gz',
            sample_bed = '/stor/work/Lambowitz/cdw2854/cfNA/tgirt_map/bed_files/merged_bed/unfragmented.bed.gz',
            tblout = '/stor/work/Lambowitz/cdw2854/cfNA/tgirt_map/bed_files/merged_bed/MACS2/annotated/unfragmented.tblout',
            phyloP = '/stor/work/Lambowitz/ref/hg19_ref/phyloP/hg19.100way.phastCons.bw'):
        self.fa = pysam.Fastafile(genome)
        self.gene_bed = gene_bed
        self.RNAshapes = RNAshapes
        self.sample_bed = pysam.Tabixfile(sample_bed)
        self.phyloP = pbw.open(phyloP)
        self.tblout = tblout
        self.mRNA_filter = mRNAFilter()
        self.gene_bed_columns = ['chrom','start','end', 'gene_name',
                                'gene_score','strand','gene_type','gene_id']
        self.gene_bed_tab = pysam.Tabixfile(gene_bed)

    def fetch_seq(self, chrom, start, end, strand):
        seq = self.fa.fetch(chrom, int(start), int(end))
        return seq if strand == "+" else reverse_complement(seq)

    
    def add_rfam(self, peak_df):
        tbl = read_tbl(self.tblout)\
            .pipe(lambda d: d[d['E-value'] < 0.01])\
            .filter(regex='name')\
            .assign(peak_name = lambda d: d['query name'].str.extract('_(chr[0-9XY]+:[0-9]+\-[0-9]+)\(')) \
            .drop(['query name'],axis=1)\
            .rename(columns = {'target name':'rfam'})
        return peak_df.merge(tbl, on = 'peak_name', how = 'left') \
                .fillna('.')

    
    def abstract_structure(self, structure):
        if set(structure) == {'(', ')', '.'}:
            s = subprocess.check_output([self.RNAshapes,'-D',structure,'-t','5']).decode()
        else:
            s = ''
        return s.strip()

    def RNAfold(self, seq, return_energy=False):
        fold = ''
        energy = 0
        if seq != '':
            fold, energy = RNA.fold(seq)
        return energy if return_energy else fold

    def full_length(self, chrom, start, end, strand):
        frag_count = 0
        fulllength = 0
        start, end = int(start), int(end)
        for frag in self.sample_bed.fetch(chrom, start, end):
            fields = frag.split('\t')
            frag_strand = fields[5]
            if frag_strand == strand:
                frag_count += 1
                if start -5 < int(fields[1]) < start + 5 and end -5 < int(fields[2]) < end + 5:
                    fulllength += 1

        if frag_count == 0:
            return 0
        return fulllength


    def mirtron_filter(self, peak_tab, remove_intron=True):
        final_columns = peak_tab.columns 
        needed_columns = ['chrom','start', 'end','peakname','pileup','strand']
        extra_columns = set(final_columns) - set(needed_columns)
        needed_columns.extend(list(extra_columns))
        needed_columns.extend(['intron_chrom',
                                'intron_start',
                                'intron_end',
                                'intron_name',
                                'intron_score',
                                'intron_strand',
                                'intron',
                                'overlapped'])
        
        intersected = BedTool()\
            .from_dataframe(peak_tab.filter(needed_columns))\
            .intersect('/stor/work/Lambowitz/ref/hg19_ref/genes/introns.gencode.bed.gz', 
                    f= 0.5,F=0.9, wao = True)\
            .to_dataframe(names = needed_columns)  \
            .pipe(lambda d: pd.concat([d.query('intron_chrom == "."'),
                                        d\
                                            .query('intron_chrom != "."')\
                                            .assign(fulllength = lambda d: list(map(self.full_length, d.intron_chrom, d.intron_start, d.intron_end, d.strand)))\
                                            .assign(intron_chrom = lambda d: np.where(d.fulllength < 1, '.',d.intron_chrom))\
                                            .drop('fulllength', axis=1)]))

        if remove_intron:
            return intersected \
                .query('intron_chrom=="."')\
                .pipe(lambda d: d.drop(list(d.filter(regex='^intron|overlap')), axis=1))\
                .drop_duplicates()
        else:
            return intersected\
                .assign(is_intron = lambda d: np.where(d.intron_chrom!=".", 'full-length intron','.'))\
                .pipe(lambda d: d.drop(list(d.filter(regex='^intron|overlap')), axis=1))\
                .drop_duplicates()

    def filter_mRNA(self, peak_tab):
        return peak_tab \
            .assign(is_exon = lambda d: [self.mRNA_filter.search(chrom, start, end, attribute = 'exon') for chrom, start, end in zip(d.chrom, d.start, d.end)]) \
            .assign(is_transcriptome_peak = lambda d: [self.mRNA_filter.search(chrom, start, end, attribute = 'transcriptome') for chrom, start, end in zip(d.chrom, d.start, d.end)])\
            .pipe(lambda d: d[(d.is_exon=="no") | ((d.is_exon=="yes") & (d.is_transcriptome_peak=="yes"))])

    def filter_gene(self, peak_tab, gene_regex='^HB[ABDGEMQP]$|^HB[ABDGEMQP][0-9]+$|TMSB4X|FTLP3$|RPS29$|RPL6P27'):
        final_columns = peak_tab.columns 
        needed_columns = ['chrom','start', 'end','peakname','pileup','strand']
        extra_columns = set(final_columns) - set(needed_columns)
        needed_columns.extend(list(extra_columns))
        peak_bed = peak_tab.filter(needed_columns)
        peaks = BedTool().from_dataframe(peak_bed)

        bed = pd.read_csv(self.gene_bed, sep='\t',
                            names = self.gene_bed_columns)\
            .pipe(lambda d: d[d.gene_name.str.contains(gene_regex)])
        gene = BedTool().from_dataframe(bed)
        return peaks.intersect(b = gene, v=True) \
            .to_dataframe(names = needed_columns)
    

    def add_genename(self, chrom, start, end, strand):
        gene_name = ''
        max_overlap = 0
        start, end = int(start), int(end)
        for gene in self.gene_bed_tab.fetch(chrom, start, end):
            fields = gene.strip().split('\t')
            name = fields[3]
            gstart, gend = int(fields[1]), int(fields[2])
            overlap = min(gend,end) - max(start, gstart) 
            if overlap > max_overlap and strand == fields[5]:
                max_overlap = overlap
                gene_name = name
        return gene_name
        
    def cloverleaf(self, seq, return_df=False):
        if not seq:
            return 'N/A'

        cmd = 'echo '+seq+ \
            '| '+ self.RNAshapes + ' -e 20  -m "[[][][]]"' 
        ps = subprocess.Popen(cmd, shell=True, 
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        res = ps.communicate()[0].decode() 
        if 'not found' in res:
            return 'N/A'
        else:
            res_list = re.sub('\s+','\t',res.strip().split('\n')[1]).split('\t')
            return res_list[1]
        
    def uncharacterized_peak(self, peak_df, remove_intron=True):
        rows = []
        for i, row in peak_df.iterrows():
            is_long_RNA = row['sense_gtype'] == 'Long RNA' 
            is_intron = row['is_intron'] != '.' or row['rfam']!='.'
            is_antisense_peak = row['sense_gtype'] == "." and row['antisense_gtype'] != 'RBP' 
            is_unannotated = row['sense_gtype'] == '.' and row['antisense_gtype'] == "."
            if is_long_RNA or is_intron or is_antisense_peak or is_unannotated:
                rows.append(row)
        return pd.DataFrame(rows)

    
    def __add_wps__(self, chrom, start, end, strand):
        window = 10
        wps = WPS(start, end, window = window)
        for fragment in self.sample_bed.fetch(chrom, int(start), int(end)):
            fields = fragment.strip().split('\t')
            frag_chrom, frag_start, frag_end, frag_strand = itemgetter(0,1,2,5)(fields)
            if strand == frag_strand:
                wps.add_fragment(frag_start, frag_end)
        max_WPS = wps.WPS_array[wps.WPS_array > 0.8* wps.WPS_array.max()]
        return len(max_WPS)/(len(wps.WPS_array) - window)


    def wps_function(self, peak_df):
        return peak_df \
            .assign(max_wps = lambda d: list(map(self.__add_wps__, d.chrom, d.start, d.end, d.strand)))


    def add_phyloP(self, chrom, start, end):
        return self.phyloP.values(chrom, int(start), int(end), numpy=True).mean()


def label_sense(picked_type_sense, picked_type_anti):
    label = '.'
    if picked_type_sense not in ['.','Unannotated']:
        label = 'Sense'
    elif picked_type_anti not in ['.','Unannotated'] :
        label = "Antisense"
    else:
        label = 'Unannotated'
    return label


def __fold_random__(sequence_bases):
    random.shuffle(sequence_bases)
    f, e = RNA.fold(''.join(sequence_bases))
    return e


@lru_cache(maxsize=128)
def get_folding_pvalue(simulation, seq):
    if seq != '':
        folded, energy = RNA.fold(seq)
        sequence_bases = list(seq)
                
        random_energies = [__fold_random__(sequence_bases) for sim in range(simulation)]
        random_energies = np.array(random_energies)
        return len(random_energies[random_energies <= energy])/simulation
    else:
        return 1


def concensus_module():
    workdir = '/stor/work/Lambowitz/cdw2854/cfNA/tgirt_map'
    fwd = workdir + '/bed_files/merged_bed/coverage/unfragmented.fwd.bigWig'
    rvs = workdir + '/bed_files/merged_bed/coverage/unfragmented.rvs.bigWig'
    bam = '/stor/work/Lambowitz/cdw2854/cfNA/tgirt_map/merged_bam/dedup/unfragmented.chrM_filter.dedup.bam'
    return concensus(bam, coverage_files = [fwd, rvs])

def main(remove_intron=True):
    WORK_DIR = os.environ['WORK'] + '/cdw2854/cfNA/tgirt_map/bed_files/merged_bed/MACS2/annotated'
    ANNOT_PEAK_FILE = WORK_DIR + '/unfragmented.filtered.tsv'
    peak_analyze = PeakAnalyzer()
    concensus_analyzer = concensus_module()
    exon_filter = ExonFilter()
    gi = GenicIntersect()

    out_columns = ['peak_name','is_sense','gname','sense_gtype', 'strand','pileup','sample_count', 'seq', 'energy',
                 'concensus_seq', 'concensus_fold', 'concensus_energy', #'concensus_folding_pval',
                 'force_cloverleaf','is_exon','is_transcriptome_peak','rfam', 'gtype'] 
    if not remove_intron:
        out_columns.append('is_intron')

    p = Pool(24)
    fold_p = partial(get_folding_pvalue, 2001)
    peak_df = pd.read_csv(ANNOT_PEAK_FILE, sep='\t') \
        .query('sample_count >= 5 & pileup >= 5') \
        .fillna('.')\
        .assign(peak_name = lambda d: d.chrom +':'+ d.start.astype(str) +'-'+ d.end.astype(str)) \
        .pipe(peak_analyze.add_rfam)\
        .pipe(peak_analyze.mirtron_filter, remove_intron=remove_intron) \
        .pipe(peak_analyze.filter_gene)\
        .pipe(exon_filter.filter, f = 0.1)\
        .pipe(peak_analyze.uncharacterized_peak)\
        .assign(psi = lambda d: list(map(gi.compute_psi, d.chrom ,d.start, d.end, d.strand)) )\
        .pipe(gi.intersect) \
        .pipe(gi.resolve_genic) \
        .assign(concensus_seq = lambda d: list(map(concensus_analyzer.find_concensus, d.chrom, d.start, d.end, d.strand)))\
        .assign(seq = lambda d: list(map(peak_analyze.fetch_seq, d.chrom, d.start, d.end, d.strand)))\
        .assign(concensus_seq = lambda d: np.where(d.is_intron==".",d.concensus_seq,d.seq)) \
        .assign(fold = lambda d: d.seq.map(peak_analyze.RNAfold))\
        .assign(energy = lambda d: d.seq.map(lambda x: peak_analyze.RNAfold(x, return_energy=True)))\
        .assign(concensus_fold = lambda d: d.concensus_seq.map(peak_analyze.RNAfold))\
        .assign(concensus_energy = lambda d: d.concensus_seq.map(lambda x: peak_analyze.RNAfold(x, return_energy=True)))\
        .assign(fold = lambda d: d.fold.map(peak_analyze.abstract_structure)) \
        .assign(concensus_fold = lambda d: d.concensus_fold.map(peak_analyze.abstract_structure)) \
        .assign(force_cloverleaf = lambda d: d.concensus_seq.map(peak_analyze.cloverleaf)) \
        .assign(gname = lambda d: np.where(d.sense_gname != ".", 
                                        d.sense_gname,
                                        d.antisense_gname)) \
        .assign(is_sense = lambda d: list(map(label_sense, d.sense_gname, d.antisense_gname))) \
        .filter(out_columns)\
        .rename(columns = {'pileup':'fragment_count'}) \
        .pipe(lambda d: d[~d.peak_name.isin(['chrX:44654061-44654153', 
                                            'chr11:74457163-74457239',
                                            'chr16:31173304-31173386'])])
        #.assign(concensus_folding_pval = lambda d: p.map(fold_p, d.concensus_seq)) \
        #.pipe(peak_analyze.filter_mRNA)\
    p.close()
    p.join()
    out_table = WORK_DIR + '/supp_tab.tsv'
    if not remove_intron:
        out_table = WORK_DIR + '/supp_tab_intron.tsv'
    peak_df.to_csv(out_table, index=False, sep='\t')
    print('Written: ', out_table)
        


if __name__ == '__main__':
    main(remove_intron=False)