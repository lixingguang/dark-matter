import six
from os import unlink
from collections import Counter
from itertools import chain
from hashlib import md5

from Bio.Seq import translate
from Bio.Data.IUPACData import (
    ambiguous_dna_complement, ambiguous_rna_complement)

from dark.aa import AA_LETTERS
from dark.filter import TitleFilter
from dark.aa import PROPERTIES, PROPERTY_DETAILS, NONE
from dark.gor4 import GOR4


def _makeComplementTable(complementData):
    """
    Make a sequence complement table.

    @param complementData: A C{dict} whose keys and values are strings of
        length one. A key, value pair indicates a substitution that should
        be performed during complementation.
    @return: A 256 character string that can be used as a translation table
        by the C{translate} method of a Python string.
    """
    table = list(range(256))
    for _from, to in complementData.items():
        table[ord(_from[0])] = ord(to[0])
    return ''.join(map(chr, table))


class Read(object):
    """
    Hold information about a single read.

    @param id: A C{str} describing the read.
    @param sequence: A C{str} of sequence information (might be
        nucleotides or proteins).
    @param quality: An optional C{str} of phred quality scores. If not C{None},
        it must be the same length as C{sequence}.
    @raise ValueError: if the length of the quality string (if any) does not
        match the length of the sequence.
    """
    ALPHABET = None

    def __init__(self, id, sequence, quality=None):
        if quality is not None and len(quality) != len(sequence):
            raise ValueError(
                'Invalid read: sequence length (%d) != quality length (%d)' %
                (len(sequence), len(quality)))

        self.id = id
        self.sequence = sequence
        self.quality = quality

    def __eq__(self, other):
        return (self.id == other.id and
                self.sequence == other.sequence and
                self.quality == other.quality)

    def __ne__(self, other):
        return not self == other

    def __len__(self):
        return len(self.sequence)

    def __hash__(self):
        """
        Calculate a hash key for a read.

        @return: The C{int} hash key for the read.
        """
        if self.quality is None:
            return hash(md5(self.id.encode('UTF-8') + b'\0' +
                            self.sequence.encode('UTF-8')).digest())
        else:
            return hash(md5(self.id.encode('UTF-8') + b'\0' +
                            self.sequence.encode('UTF-8') + b'\0' +
                            self.quality.encode('UTF-8')).digest())

    def toString(self, format_):
        """
        Convert the read to a string format.

        @param format_: Either 'fasta' or 'fastq'.
        @raise ValueError: if C{format_} is 'fastq' and the read has no quality
            information, or if an unknown format is requested.
        @return: A C{str} representing the read in the requested format.
        """
        if format_ == 'fasta':
            return '>%s\n%s\n' % (self.id, self.sequence)
        elif format_ == 'fastq':
            if self.quality is None:
                raise ValueError('Read %r has no quality information' %
                                 self.id)
            else:
                return '@%s\n%s\n+%s\n%s\n' % (
                    self.id, self.sequence, self.id, self.quality)
        else:
            raise ValueError("Format must be either 'fasta' or 'fastq'.")

    def lowComplexityFraction(self):
        """
        What fraction of a read's bases are in low-complexity regions?
        By convention, a region of low complexity is indicated by lowercase
        base letters.

        @return: The C{float} representing the fraction of bases in the
            read that are in regions of low complexity.
        """
        length = len(self)
        if length:
            lowerCount = len(list(filter(str.islower, self.sequence)))
            return float(lowerCount) / length
        else:
            return 0.0

    def walkHSP(self, hsp):
        """
        Provide information about exactly how a read matches a subject, as
        specified by C{hsp}.

        @return: A generator that yields (offset, residue, inMatch) tuples.
            The offset is the offset into the matched subject. The residue is
            the base in the read (which might be '-' to indicate a gap in the
            read was aligned with the subject at this offset). inMatch will be
            C{True} for residues that are part of the HSP match, and C{False}
            for the (possibly non-existent) parts of the read that fall outside
            the HSP (aka, the "whiskers" in an alignment graph).
        """

        # It will be easier to understand the following implementation if
        # you refer to the ASCII art illustration of an HSP in the
        # dark.hsp._Base class in hsp.py

        # Left whisker.
        readOffset = 0
        subjectOffset = hsp.readStartInSubject
        while subjectOffset < hsp.subjectStart:
            yield (subjectOffset, self.sequence[readOffset], False)
            readOffset += 1
            subjectOffset += 1

        # Match.
        for matchOffset, residue in enumerate(hsp.readMatchedSequence):
            yield (subjectOffset + matchOffset, residue, True)

        # Right whisker.
        readOffset = hsp.readEnd
        subjectOffset = hsp.subjectEnd
        while subjectOffset < hsp.readEndInSubject:
            yield (subjectOffset, self.sequence[readOffset], False)
            readOffset += 1
            subjectOffset += 1

    def checkAlphabet(self, count=10):
        """
        A function which checks whether the sequence in a L{dark.Read} object
        corresponds to its readClass. For AA reads, more testing is done in
        dark.Read.AARead.checkAlphabet.

        @param count: An C{int}, indicating how many bases or amino acids at
            the start of the sequence should be considered. If C{None}, all
            bases are checked.
        @return: C{True} if the alphabet characters in the first C{count}
            positions of sequence is a subset of the allowed alphabet for this
            read class, or if the read class has a C{None} alphabet.
        @raise ValueError: If the sequence alphabet is not a subset of the read
            class alphabet.
        """
        if count is None:
            readLetters = set(self.sequence.upper())
        else:
            readLetters = set(self.sequence.upper()[:count])
        # Check if readLetters is a subset of self.ALPHABET.
        if self.ALPHABET is None or readLetters.issubset(self.ALPHABET):
            return readLetters
        raise ValueError("Read alphabet (%r) is not a subset of expected "
                         "alphabet (%r) for read class %s." % (
                             ''.join(sorted(readLetters)),
                             ''.join(sorted(self.ALPHABET)),
                             str(self.__class__.__name__)))


class _NucleotideRead(Read):
    """
    Holds methods to work with nucleotide (DNA and RNA) sequences.
    """
    def translations(self):
        """
        Yield all six translations of a nucleotide sequence.

        @return: A generator that produces six L{TranslatedRead} instances.
        """
        rc = self.reverseComplement().sequence
        for reverseComplemented in False, True:
            for frame in 0, 1, 2:
                seq = rc if reverseComplemented else self.sequence
                # Get the suffix of the sequence for translation. I.e.,
                # skip 0, 1, or 2 initial bases, depending on the frame.
                # Note that this makes a copy of the sequence, which we can
                # then safely append 'N' bases to to adjust its length to
                # be zero mod 3.
                suffix = seq[frame:]
                lengthMod3 = len(suffix) % 3
                if lengthMod3:
                    suffix += ('NN' if lengthMod3 == 1 else 'N')
                yield TranslatedRead(self, translate(suffix), frame,
                                     reverseComplemented)

    def reverseComplement(self):
        """
        Reverse complement a nucleotide sequence.

        @return: The reverse complemented sequence as an instance of the
            current class.
        """
        quality = None if self.quality is None else self.quality[::-1]
        sequence = self.sequence.translate(self.COMPLEMENT_TABLE)[::-1]
        return self.__class__(self.id, sequence, quality)


class DNARead(_NucleotideRead):
    """
    Hold information and methods to work with DNA reads.
    """
    ALPHABET = set('ATCG')

    COMPLEMENT_TABLE = _makeComplementTable(ambiguous_dna_complement)


class RNARead(_NucleotideRead):
    """
    Hold information and methods to work with RNA reads.
    """
    ALPHABET = set('ATCGU')

    COMPLEMENT_TABLE = _makeComplementTable(ambiguous_rna_complement)


# Keep a single GOR4 instance that can be used by all AA reads. This saves us
# from re-scanning the GOR IV secondary structure database every time we make
# an AARead instance.
_GOR4 = GOR4()


class AARead(Read):
    """
    Hold information and methods to work with AA reads.
    """
    ALPHABET = set(AA_LETTERS)

    def checkAlphabet(self, count=10):
        """
        A function which checks if an AA read really contains amino acids. This
        additional testing is needed, because the letters in the DNA alphabet
        are also in the AA alphabet.

        @param count: An C{int}, indicating how many bases or amino acids at
            the start of the sequence should be considered. If C{None}, all
            bases are checked.
        @return: C{True} if the alphabet characters in the first C{count}
            positions of sequence is a subset of the allowed alphabet for this
            read class, or if the read class has a C{None} alphabet.
        @raise ValueError: If a DNA sequence has been passed to AARead().
        """
        if six.PY3:
            readLetters = super().checkAlphabet(count)
        else:
            readLetters = Read.checkAlphabet(self, count)
        if len(self) > 10 and readLetters.issubset(set('ACGT')):
            raise ValueError('It looks like a DNA sequence has been passed to '
                             'AARead().')
        return readLetters

    def properties(self):
        """
        Translate an amino acid sequence to properties of the form:
        'F': HYDROPHOBIC | AROMATIC.

        @return: A generator yielding properties for the residues in the
            current sequence.
        """
        return (PROPERTIES.get(aa, NONE) for aa in self.sequence)

    def propertyDetails(self):
        """
        Translate an amino acid sequence to properties. Each property of the
        amino acid gets a value scaled from -1 to 1.

        @return: A list of property dictionaries.
        """
        return (PROPERTY_DETAILS.get(aa, NONE) for aa in self.sequence)

    def ORFs(self):
        """
        Find all ORFs in our sequence.

        @return: A generator that yields AAReadORF instances that correspond
            to the ORFs found in the AA sequence.
        """
        ORFStart = None
        openLeft = True
        seenStart = False

        for index, residue in enumerate(self.sequence):
            if residue == 'M':
                # Start codon.
                openLeft = False
                seenStart = True
            elif residue == '*':
                # Stop codon. Yield an ORF, if it has non-zero length.
                if ORFStart is not None and index - ORFStart > 0:
                    # The ORF has non-zero length.
                    yield AAReadORF(self, ORFStart, index, openLeft, False)
                    ORFStart = None
                # After a stop codon, we can no longer be open on the left
                # and we have no longer seen a start codon.
                openLeft = seenStart = False
            else:
                if (seenStart or openLeft) and ORFStart is None:
                    ORFStart = index

        # End of sequence. Yield the final ORF if there is one and it has
        # non-zero length.
        length = len(self.sequence)
        if ((seenStart or openLeft) and ORFStart is not None
                and length - ORFStart > 0):
            yield AAReadORF(self, ORFStart, length, openLeft, True)

    def gor4(self):
        """
        Get GOR IV secondary structure predictions (and the associated
        prediction probabilities).

        @return: A C{dict} with 'predictions' and 'probabilities' keys.
            The 'predictions' value is a C{str} of letters from {'H', 'E',
            'C'} for Helix, Beta Strand, Coil.  The probabilities value is
            a C{list} of C{float} triples, one for each amino acid in
            C{sequence}. The C{float} values are the probabilities assigned,
            in order, to Helix, Beta Strand, Coil.
        """
        return _GOR4.predict(self.sequence)


class AAReadWithX(AARead):
    """
    Hold information and methods to work with AA reads with additional
    characters.
    """
    ALPHABET = set(AA_LETTERS + ['X'])


class AAReadORF(AARead):
    """
    Hold information about an ORF from an AA read.

    @param originalRead: The original L{AARead} instance in which this ORF
        occurs.
    @param start: The C{int} offset where the ORF starts in the original read.
    @param stop: The Python-style C{int} offset of the end of the ORF in the
        original read. The final index is not included in the ORF.
    @param openLeft: A C{bool}. If C{True}, the ORF potentially begins before
        the sequence given in C{sequence}. I.e., the ORF-detection code started
        to examine a read assuming it was already in an ORF. If C{False}, a
        start codon was found preceeding this ORF.
    @param openRight: A C{bool}. If C{True}, the ORF potentially ends after
        the sequence given in C{sequence}. I.e., the ORF-detection code
        was in an ORF when it encountered the end of a read (so no stop codon
        was found). If C{False}, a stop codon was found in the read after this
        ORF.
    """
    def __init__(self, originalRead, start, stop, openLeft, openRight):
        if start < 0:
            raise ValueError('start offset (%d) less than zero' % start)
        if stop > len(originalRead):
            raise ValueError('stop offset (%d) > original read length (%d)' %
                             (stop, len(originalRead)))
        if start > stop:
            raise ValueError('start offset (%d) greater than stop offset (%d)'
                             % (start, stop))
        newId = '%s-%s%d:%d%s' % (originalRead.id,
                                  '(' if openLeft else '[',
                                  start, stop,
                                  ')' if openRight else ']')
        AARead.__init__(self, newId, originalRead.sequence[start:stop])
        self.start = start
        self.stop = stop
        self.openLeft = openLeft
        self.openRight = openRight


class SSAARead(AARead):
    """
    Hold information to work with AAReads that have secondary structure
    information attached to them.

    Note that this class (currently) has no quality string associated with it.

    @param id: A C{str} describing the read.
    @param sequence: A C{str} of sequence information.
    @param structure: A C{str} of structure information.
    """
    def __init__(self, id, sequence, structure):
        if six.PY3:
            super().__init__(id, sequence)
        else:
            AARead.__init__(self, id, sequence)
        self.structure = structure

    def __eq__(self, other):
        return (self.id == other.id and
                self.sequence == other.sequence and
                self.structure == other.structure)

    def __getitem__(self, item):
        sequence = self.sequence[item]
        structure = None if self.structure is None else self.structure[item]
        return self.__class__(self.id, sequence, structure)


class TranslatedRead(AARead):
    """
    Hold information about one DNA->AA translation of a Read.

    @param originalRead: The original DNA or RNA L{Read} instance from which
        this translation was obtained.
    @param sequence: The C{str} AA translated sequence.
    @param frame: The C{int} frame, either 0, 1, or 2.
    @param reverseComplemented: A C{bool}, C{True} if the original sequence
        must be reverse complemented to obtain this AA sequence.
    """
    def __init__(self, originalRead, sequence, frame,
                 reverseComplemented=False):
        if frame not in (0, 1, 2):
            raise ValueError('Frame must be 0, 1, or 2')
        newId = '%s-frame%d%s' % (originalRead.id, frame,
                                  'rc' if reverseComplemented else '')
        AARead.__init__(self, newId, sequence)
        self.frame = frame
        self.reverseComplemented = reverseComplemented

    def __eq__(self, other):
        return (AARead.__eq__(self, other) and
                self.frame == other.frame and
                self.reverseComplemented == other.reverseComplemented)

    def __ne__(self, other):
        return not self == other

    def maximumORFLength(self):
        """
        Return the length of the longest (possibly partial) ORF in a translated
        read. The ORF may originate or terminate outside the sequence, which is
        why the length is just a lower bound.
        """
        return max(len(orf) for orf in self.ORFs())


class Reads(object):
    """
    Maintain a collection of sequence reads.

    @param initialReads: If not C{None}, an iterable of C{Read} (or C{Read}
        subclass) instances.
    """

    def __init__(self, initialReads=None):
        self.additionalReads = []
        self._length = 0
        if initialReads is not None:
            list(map(self.add, initialReads))

    def add(self, read):
        """
        Add a read to this collection of reads.

        @param read: A C{Read} instance.
        """
        self.additionalReads.append(read)
        self._length += 1

    def __iter__(self):
        # Reset self._length because __iter__ may be called more than once.
        self._length = 0
        for read in chain(self.iter(), self.additionalReads):
            self._length += 1
            yield read

    def iter(self):
        """
        Placeholder to allow subclasses to provide reads.
        """
        return []

    def __len__(self):
        # Note that __len__ reflects the number of reads that are currently
        # known.  If self.__iter__ has not been exhausted, we will not know
        # the true number of reads.
        return self._length

    def save(self, filename, format_='fasta'):
        """
        Write the reads to C{filename} in the requested format.

        @param filename: Either a C{str} file name to save into (the file will
            be overwritten) or an open file descriptor (e.g., sys.stdout).
        @param format_: A C{str} format to save as, either 'fasta' or 'fastq'.
        @raise ValueError: if C{format_} is 'fastq' and a read with no quality
            is present, or if an unknown format is requested.
        @return: C{self} in case our caller wants to chain the result into
            another method call.
        """
        format_ = format_.lower()

        if isinstance(filename, str):
            try:
                with open(filename, 'w') as fp:
                    for read in self:
                        fp.write(read.toString(format_))
            except ValueError:
                unlink(filename)
                raise
        else:
            # We have a file-like object.
            for read in self:
                filename.write(read.toString(format_))
        return self

    def filter(self, minLength=None, maxLength=None, removeGaps=False,
               whitelist=None, blacklist=None,
               titleRegex=None, negativeTitleRegex=None,
               truncateTitlesAfter=None, indices=None, head=None,
               removeDuplicates=False, modifier=None):
        """
        Filter a set of reads to produce a matching subset.

        Note: there are many additional filtering options that could be added,
        e.g., filtering on read id (whitelist, blacklist, regex, etc), GC %,
        and quality.

        @param minLength: The minimum acceptable length.
        @param maxLength: The maximum acceptable length.
        @param removeGaps: If C{True} remove all gaps ('-' characters) from the
            read sequences.
        @param whitelist: If not C{None}, a set of exact read ids that are
            always acceptable (though other characteristics, such as length,
            of a whitelisted id may rule it out).
        @param blacklist: If not C{None}, a set of exact read ids that are
            never acceptable.
        @param titleRegex: A regex that read ids must match.
        @param negativeTitleRegex: A regex that read ids must not match.
        @param truncateTitlesAfter: A string that read ids will be truncated
            beyond. If the truncated version of an id has already been seen,
            that sequence will be skipped.
        @param indices: Either C{None} or a set of C{int} indices corresponding
            to reads that are wanted. Indexing starts at zero.
        @param head: If not C{None}, the C{int} number of sequences at the
            start of the reads to return. Later sequences are skipped.
        @param removeDuplicates: If C{True} remove duplicated sequences.
        @param modifier: If not C{None}, a function that is passed a read
            and which either returns a read or C{None}. If it returns a read,
            that read is passed through the filter. If it returns C{None},
            the read is omitted. Such a function can be used to do customized
            filtering, to change sequence ids, etc.
        @return: A new C{Reads} instance, with reads filtered as requested.
        """
        return Reads(self._filter(
            minLength=minLength, maxLength=maxLength, removeGaps=removeGaps,
            whitelist=whitelist, blacklist=blacklist, titleRegex=titleRegex,
            negativeTitleRegex=negativeTitleRegex,
            truncateTitlesAfter=truncateTitlesAfter, indices=indices,
            head=head, removeDuplicates=removeDuplicates, modifier=modifier))

    def _filter(self, minLength=None, maxLength=None, removeGaps=False,
                whitelist=None, blacklist=None,
                titleRegex=None, negativeTitleRegex=None,
                truncateTitlesAfter=None, indices=None, head=None,
                removeDuplicates=False, modifier=None):
        """
        Filter a set of reads to produce a matching subset.

        See docstring for self.filter (above) for parameter docs.

        @return: A generator that yields C{Read} instances.
        """
        if (whitelist or blacklist or titleRegex or negativeTitleRegex or
                truncateTitlesAfter):
            titleFilter = TitleFilter(
                whitelist=whitelist, blacklist=blacklist,
                positiveRegex=titleRegex, negativeRegex=negativeTitleRegex,
                truncateAfter=truncateTitlesAfter)
        else:
            titleFilter = None

        if removeDuplicates:
            sequencesSeen = set()

        for readIndex, read in enumerate(self):

            if head is not None and readIndex == head:
                # We're completely done.
                return

            readLen = len(read)
            if ((minLength is not None and readLen < minLength) or
                    (maxLength is not None and readLen > maxLength)):
                continue

            if removeGaps:
                sequence = read.sequence.replace('-', '')
                read = read.__class__(read.id, sequence, read.quality)

            if (titleFilter and
                    titleFilter.accept(read.id) == TitleFilter.REJECT):
                continue

            if indices is not None and readIndex not in indices:
                continue

            if removeDuplicates:
                if read.sequence in sequencesSeen:
                    continue
                sequencesSeen.add(read.sequence)

            if modifier:
                modified = modifier(read)
                if modified is None:
                    continue
                else:
                    read = modified

            yield read

    def summarizePosition(self, index):
        """
        Compute residue counts a specific sequence index.

        @param index: an C{int} index into the sequence.
        @return: A C{dict} with the count of too-short (excluded) sequences,
            and a Counter instance giving the residue counts.
        """
        countAtPosition = Counter()
        excludedCount = 0

        for read in self:
            try:
                countAtPosition[read.sequence[index]] += 1
            except IndexError:
                excludedCount += 1

        return {
            'excludedCount': excludedCount,
            'countAtPosition': countAtPosition
        }
