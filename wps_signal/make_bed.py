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

in_path = '/stor/work/Lambowitz/cdw2854/cell_Free_nucleotides/tgirt_map/bed_files'
out_path = '/stor/work/Lambowitz/cdw2854/cell_Free_nucleotides/tgirt_map/merged_bed'
beds = glob.glob(in_path + '/*bed.gz') 
beds.sort()

for regex, label in zip(['Q[Cc][Ff][0-9]+', 'Frag', 'L[12]', 'N[aA]', 'All'],
                        ['unfragmented','fragmented','polyA','alkaline', 'all']):
    samples = filter(lambda x: re.search(regex, x), beds)

    sample_df = pd.DataFrame({'bed': list(samples)}) \
            .assign(lab = lambda d: d.bed.map(label_sample))

    for lab, lab_df in sample_df.groupby('lab'):
        bed_files = lab_df.bed
        outname =  '%s/%s%s.bed.gz' %(out_path, label, lab)

        command = 'zcat {in_beds} '\
                '| sort -k1,1 -k2,2n -k3,3n '\
                '| bgzip '\
                '> {out_bed}'\
                '; tabix -p bed -f {out_bed}'\
                .format(in_beds = ' '.join(bed_files),
                        out_bed = outname)
        print (command)
