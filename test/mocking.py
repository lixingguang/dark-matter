"""
This is a slight adaptation (for __iter__) by Terry of
http://hg.python.org/cpython/file/7cfbebadb90b/Lib/unittest/mock.py#l2239
because mock.mock_open in Python 2.7 doesn't support using a file descriptor
from "with open() as fp" as an iterator.
"""

import six

try:
    from unittest.mock import MagicMock
except ImportError:
    from mock import MagicMock

file_spec = None


def _iterate_read_data(read_data):
    # Helper for mock_open:
    # Retrieve lines from read_data via a generator so that separate calls to
    # readline, read, and readlines are properly interleaved
    data_as_list = ['{}\n'.format(l) for l in read_data.split('\n')]

    if data_as_list[-1] == '\n':
        # If the last line ended in a newline, the list comprehension will
        # have an extra entry that's just a newline.  Remove this.
        data_as_list = data_as_list[:-1]
    else:
        # If there wasn't an extra newline by itself, then the file being
        # emulated doesn't have a newline to end the last line  remove the
        # newline that our naive format() added
        data_as_list[-1] = data_as_list[-1][:-1]

    for line in data_as_list:
        yield line


class NoStopIterationReadline(six.Iterator):
    """
    A class providing a readline method whose calling does not result
    in C{StopIteration} being raised.
    """
    def __init__(self, handle, data):
        self.handle = handle
        self.data = list(data)
        self.index = 0

    def readline(self):
        if self.handle.readline.return_value is not None:
            return self.handle.readline.return_value
        elif self.index < len(self.data):
            self.index += 1
            return self.data[self.index - 1]
        else:
            # Ran out of data.
            return ''

    def __next__(self):
        line = self.readline()
        if line:
            return line
        else:
            raise StopIteration

    def __iter__(self):
        return self


def mockOpen(mock=None, read_data=''):
    """
    A helper function to create a mock to replace the use of `open`. It works
    for `open` called directly or used as a context manager.

    The `mock` argument is the mock object to configure. If `None` (the
    default) then a `MagicMock` will be created for you, with the API limited
    to methods or attributes available on standard file handles.

    `read_data` is a string for the `read` methoddline`, and `readlines` of the
    file handle to return.  This is an empty string by default.
    """
    def _readlines_side_effect(*args, **kwargs):
        if handle.readlines.return_value is not None:
            return handle.readlines.return_value
        return list(_data)

    def _read_side_effect(*args, **kwargs):
        if handle.read.return_value is not None:
            return handle.read.return_value
        return ''.join(_data)

    def _readline_side_effect():
        if handle.readline.return_value is not None:
            while True:
                yield handle.readline.return_value
        for line in _data:
            yield line

    global file_spec
    if file_spec is None:
        import _io
        file_spec = list(set(dir(_io.TextIOWrapper)).union(
            set(dir(_io.BytesIO))))

    if mock is None:
        mock = MagicMock(name='open', spec=open)

    handle = MagicMock(spec=file_spec)
    handle.__enter__.return_value = handle

    _data = _iterate_read_data(read_data)

    noStopIterationReadline = NoStopIterationReadline(
        handle, _iterate_read_data(read_data))

    handle.write.return_value = None
    handle.read.return_value = None
    handle.readline.return_value = None
    handle.readlines.return_value = None
    handle.__iter__.return_value = noStopIterationReadline

    handle.read.side_effect = _read_side_effect
    handle.readline.side_effect = noStopIterationReadline.readline
    handle.readlines.side_effect = _readlines_side_effect

    mock.return_value = handle
    return mock


class File(object):
    """
    A file mock.

    @param data: A C{list} of C{str} lines the file should contain.
    """
    def __init__(self, data):
        self._data = data
        self._index = 0
        self._closed = False

    def close(self):
        self._closed = True

    def closed(self):
        return self._closed

    def read(self):
        return ''.join(self._data)

    def readline(self):
        self._index += 1
        try:
            return self._data[self._index - 1]
        except IndexError:
            return ''

    def __iter__(self):
        index = self._index
        self._index = 0
        return iter(self._data[index:])

    def __exit__(self, *args):
        pass

    def __enter__(self):
        return self

    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return 0

    def seek(self, n):
        """
        Poor man's seek that only knows how to seek to offsets that are at the
        start of a line.
        """
        count = 0
        self._index = 0

        while True:
            if self._index > len(self._data):
                raise ValueError(
                    'Cannot seek beyond end of data')
            elif count == n:
                # We have arrived.
                return
            elif count > n:
                raise ValueError(
                    'Cannot seek to position that is not a line beginning')
            else:
                # Add on the length of the next line.
                count += len(self._data[self._index])
                self._index += 1
