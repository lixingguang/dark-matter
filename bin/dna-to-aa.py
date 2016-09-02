#!/usr/bin/env python

"""
Read DNA FASTA from stdin and print AA FASTA to stdout.  If a minimum
ORF length is given, only print AA sequences that have an ORF of at least
that length.

Note that start and stop codons will be present in the output. If you actually
want to just output all ORFs, use extract-ORFs.py directly instead (or pipe
the output of this program into extract-ORFs.py --type aa).
"""

from __future__ import print_function

import sys
import argparse

from Bio.Data.CodonTable import TranslationError

from dark.reads import DNARead, RNARead
from dark.fasta import FastaReads


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Convert DNA to AA',
        epilog='Given DNA FASTA on stdin, output AA FASTA to stdout. '
        'Optionally, filter by minimum required ORF length.'
    )

    parser.add_argument(
        '--type', default='dna', choices=['dna', 'rna'],
        help='The type of the bases in the stdin FASTA.')

    parser.add_argument(
        '--minORFLength', metavar='LEN', type=int, default=None,
        help='Translations to AA that do not contain an ORF of at least '
        'this length will not be produced.')

    args = parser.parse_args()

    if args.type == 'dna':
        readClass = DNARead
    else:
        readClass = RNARead

    reads = FastaReads(sys.stdin, readClass)
    write = sys.stdout.write
    minORFLength = args.minORFLength

    for read in reads:
        try:
            for translation in read.translations():
                if (minORFLength is None or
                        translation.maximumORFLength() >= minORFLength):
                    write(translation.toString('fasta'))
        except TranslationError as error:
            print('Could not translate read %r sequence '
                  '%r (%s).' % (read.id, read.sequence, error),
                  file=sys.stderr)
            sys.exit(1)
