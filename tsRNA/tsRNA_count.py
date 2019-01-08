#!/usr/bin/env python

from multiprocessing import Pool
import os
from operator import itemgetter
from collections import defaultdict, Counter
import glob
import pandas as pd
import pysam
from sequencing_tools.bam_tools.poisson_umi_tools import correct_umi_count
from itertools import groupby
import re

def coordinate_grouper(x):
    fields = x.strip().split('\t')
    return itemgetter(0,1,2,5,6)(fields)

class tsRNA_counter():
    def __init__(self, bed, samplename):
        anticodon_table = '/stor/work/Lambowitz/ref/hg19_ref/genes/tRNA/anticodon_annotations.tsv'
        self.bed = pysam.Tabixfile(bed)
        self.aligned_contig = set(self.bed.contigs)
        self.anticodon_df = pd.read_table(anticodon_table)\
                .pipe(lambda d: d[d.tRNA.isin(self.aligned_contig)])
        self.tsRNA_counter = defaultdict(Counter)
        self.samplename = samplename
        self.splice = re.compile('([0-9]+)S')
        self.aligned = re.compile('([0-9]+)')
    
    def count_tRNA(self):
        for _, tRNA in self.anticodon_df.iterrows():
            iterator = self.bed.fetch(tRNA['tRNA'])
            for (chrom, start, end, strand, cigar) ,lines in groupby(iterator, coordinate_grouper):
                clipped = sum(map(int,self.splice.findall(cigar)))
                total_base = sum(map(int, self.aligned.findall(cigar)))

                if clipped/total_base < 0.1 and strand == '+':
                    start, end = int(start), int(end)
                    umis = set()
                    for line in lines:
                        fields = line.split('\t')
                        umi = fields[3].split('_')[0]
                        umis.add(umi)
                    read_count = correct_umi_count(len(umis), umi_nt = 6)
                    
                    start_in_anticodon = tRNA['anticodon_start'] -1 < start < tRNA['anticodon_end']+1
                    end_at3 = end > tRNA['end'] - 5 
                    start_at5 = start <  5 
                    end_in_anticodon = tRNA['anticodon_start'] - 1 < end < tRNA['anticodon_end'] + 1
                    short_frag = end - start < 23

                    if end_at3 and short_frag:
                        self.tsRNA_counter[tRNA['tRNA']]["3' tsRNA"] += read_count

                    elif end_at3 and start_in_anticodon:
                        self.tsRNA_counter[tRNA['tRNA']]["3' half"] += read_count
                    
                    elif start_at5 and end_in_anticodon:
                        self.tsRNA_counter[tRNA['tRNA']]["5' half"] += read_count
                    
                    else:
                        self.tsRNA_counter[tRNA['tRNA']]['Others'] += read_count
    
    
    def write_table(self, tablename):
        dfs = []
        for tRNA, frag_dict in self.tsRNA_counter.items():
            df = pd.DataFrame({'frag_type':list(frag_dict.keys()),
                               'frag_count':list(frag_dict.values())})\
                .assign(samplename =self. samplename)\
                .assign(tRNA = tRNA)
            dfs.append(df)

        pd.concat(dfs).reset_index(drop=True).to_feather(tablename)
        print('Written %s' %tablename)


def count_ts(sample_folder):
    bed = sample_folder + '/count_temp/small_RNA.all.bed.gz'
    samplename = os.path.basename(sample_folder)
    tablename = sample_folder + '/count_temp/tRNA_frag.feather'
    os.system('tabix -f -p bed %s' %bed)
    if not os.path.isfile(tablename):
        print('Running %s' %sample_folder)
        tsRNA = tsRNA_counter(bed, samplename)
        tsRNA.count_tRNA()
        tsRNA.write_table(tablename)
    return tablename


def main():
    project_path = '/stor/work/Lambowitz/cdw2854/cfNA/tgirt_map'
    sample_folders = glob.glob(project_path + '/*001')
    sample_folders = filter(lambda x: 'genome-sim' not in x, sample_folders)
    
    p = Pool(24)
    dfs = p.map(count_ts, sample_folders)
    p.close()
    p.join()

    tablename = project_path + '/Counts/tsRNA.feather'
    pd.concat(map(pd.read_feather, dfs))\
        .reset_index(drop=True)\
        .to_feather(tablename)
    print('Written %s' %tablename)

if __name__ == '__main__':
    main()



                




