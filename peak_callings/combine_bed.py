#!/usr/bin/env python

import os
import glob
import re
import pandas as pd

def label_sample(x):
    if 'repeat' in x:
        return '.no_sncRNA_repeat'
    elif 'sncRNA' in x:
        return '.no_sncRNA'
    else:
        return ''

def output_merge_bed(sample_df, outname):
    '''
    sample_df containing columns:
    bed: full path to bed file

    '''
    bed_files = sample_df.bed
    command = 'zcat {in_beds} '\
            '| sort -k1,1 -k2,2n -k3,3n '\
            '| bgzip '\
            '> {out_bed}'\
            '; tabix -p bed -f {out_bed}'\
            .format(in_beds = ' '.join(bed_files),
                    out_bed = outname)
    print (command)



project_path = '/stor/work/Lambowitz/cdw2854/cell_Free_nucleotides/tgirt_map'
project_path = '/stor/work/Lambowitz/cdw2854/cfNA/tgirt_map'
in_path = project_path + '/bed_files'
out_path = project_path + '/merged_bed'
beds = glob.glob(in_path + '/*bed.gz') 
beds.sort()

if not os.path.isdir(out_path):
    os.mkdir(out_path)

for regex, label in zip(['Q[Cc][Ff][0-9]+|Exo|[DE][DE]', 'Frag', 'L[12]', 'N[aA][0-9]+', 'All','Exo|[DE][DE]'],
                        ['unfragmented','fragmented','polyA','alkaline', 'all','exonuclease']):
    samples = filter(lambda x: re.search(regex, os.path.basename(x).split('no')[0]), beds)

    sample_df = pd.DataFrame({'bed': list(samples)}) \
            .assign(lab = lambda d: d.bed.map(label_sample))

    for lab, lab_df in sample_df.groupby('lab'):
        outname =  '%s/%s%s.bed.gz' %(out_path, label, lab)
        output_merge_bed(lab_df, outname)


    if label == 'unfragmented':
        sub_sample = sample_df.query('lab == ""') \
            .assign(rep_num = lambda d: d.index.map(lambda x: x%2))
        
        for (lab, rep_num), subsample_df in sub_sample.groupby(['lab','rep_num']):
            outname =  '%s/%s%s_%i.bed.gz' %(out_path, label, lab, rep_num + 1)
            output_merge_bed(subsample_df, outname)

