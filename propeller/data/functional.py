#   Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import print_function
from __future__ import absolute_import
from __future__ import unicode_literals

import sys
import logging
import os
import itertools
import random
import inspect
import multiprocessing
from contextlib import contextmanager
import gzip
import struct
import functools

import six
from six.moves import zip, map, filter
import numpy as np

from propeller.util import map_structure

log = logging.getLogger(__name__)

__all__ = ['Dataset']


@contextmanager
def open_file(filename, format=None):
    if format is None:
        fd = open(filename, 'rb')
    elif format == 'GZIP':
        fd = gzip.open(filename, 'rb')
    else:
        raise ValueError('unkwon file format %s' % format)
    yield fd
    fd.close()


def open_record(filename):
    def gen():
        with open_file(filename, format='GZIP') as f:
            while True:
                data = f.read(struct.calcsize('i'))
                if not len(data):
                    raise StopIteration
                l, = struct.unpack('i', data)
                data = f.read(l)
                yield data

    return gen


def shuffle_func(dataset, buffer_size):
    def gen():
        buf = []
        iterable = dataset()
        try:
            while len(buf) < buffer_size:
                buf.append(next(iterable))
            while 1:
                i = random.randint(0, buffer_size - 1)
                n = next(iterable)
                yield buf[i]
                buf[i] = n
        except StopIteration:
            if len(buf):
                random.shuffle(buf)
                for i in buf:
                    yield i

    return gen


def interleave_func(iterable, map_fn, cycle_length, block_length):
    def gen():
        ls = itertools.tee(iterable(), cycle_length)
        buf = []
        for i, j in enumerate(ls):
            j = itertools.islice(j, i, None, cycle_length)
            j = map(map_fn, j)
            j = (jjj for jj in j for jjj in jj)  #flatten
            buf.append(j)

        for tup in six.moves.zip_longest(*buf):
            for ii in (i for i in tup if i is not None):
                yield ii

    return gen


def repeat_func(dataset, n):
    def gen():
        iterable = dataset()
        if n >= 0:
            ret = itertools.chain(*itertools.tee(iterable, n))
        else:
            ret = itertools.cycle(iterable)

        for i in ret:
            yield i

    return gen


def filter_func(dataset, fn):
    def gen():
        for i in dataset():
            if isinstance(i, tuple) or isinstance(i, list):
                if fn(*i) is True:
                    yield i
            else:
                if fn(i) is True:
                    yield i

    return gen


def map_func(dataset, fn):
    def gen():
        for i in dataset():
            if isinstance(i, tuple) or isinstance(i, list):
                yield fn(*i)
            else:
                yield fn(i)

    return gen


def shard_func(dataset, num_shards, index):
    def gen():
        iterable = dataset()
        ret = itertools.islice(iterable, index, None, num_shards)
        for i in ret:
            yield i

    return gen


def take_func(dataset, count):
    def gen():
        iterable = dataset()
        ret = itertools.islice(iterable, count)
        for i in ret:
            yield i

    return gen


def buffered_func(dataset, size):
    """
    Creates a buffered data reader.

    The buffered data reader will read and save data entries into a
    buffer. Reading from the buffered data reader will proceed as long
    as the buffer is not empty.

    :param reader: the data reader to read from.
    :type reader: callable
    :param size: max buffer size.
    :type size: int

    :returns: the buffered data reader.
    """

    class EndSignal():
        pass

    end = EndSignal()

    def read_worker(r, q):
        for d in r:
            q.put(d)
        q.put(end)

    def data_reader():
        r = dataset()
        q = multiprocessing.Queue(maxsize=size)
        t = multiprocessing.Process(
            target=read_worker, args=(
                r,
                q, ))
        t.daemon = True
        t.start()
        e = q.get()
        while e != end:
            yield e
            e = q.get()

    return data_reader


def padded_batch_func(dataset, batch_size, pad_value=0, max_seqlen=None):
    if not isinstance(batch_size, int):
        raise ValueError('unknown batch_size: %s' % repr(batch_size))

    def gen():
        iterable = dataset()
        pad_value_t = pad_value
        while True:
            buf = list(itertools.islice(iterable, batch_size))
            if not len(buf):
                raise StopIteration
            buf = list(zip(*buf))  # transpose
            if type(pad_value_t) not in [list, tuple]:
                pad_value_t = [pad_value_t] * len(buf)
            padded = []
            assert len(buf) == len(
                pad_value_t), 'pad_value [%d] != element size[%d]' % (
                    len(pad_value_t), len(buf))
            for e, pv in zip(buf, pad_value_t):
                elem = e[0]
                if (not np.isscalar(elem)) and elem.shape != ():
                    max_len = max(map(len,
                                      e)) if max_seqlen is None else max_seqlen
                    e = map(lambda i: np.pad(i, [0, max_len - len(i)], 'constant', constant_values=pv) if max_len >= len(i) else i[: max_len], e)
                padded.append(np.stack(list(e)))
            yield padded

    return gen


class Dataset(object):
    @classmethod
    def from_generator_func(cls, gen, data_shapes=None, data_types=None):
        if not inspect.isgeneratorfunction(gen):
            raise ValueError('expect generator function, got %s' % repr(gen))

        def wrapper():  #compat to py3.7
            try:
                for item in gen():
                    yield item
            except RuntimeError as e:
                if str(e) != 'generator raised StopIteration':
                    raise e

        ret = cls()
        ret.generator = wrapper
        ret.data_shapes = data_shapes
        ret.data_types = data_types
        return ret

    @classmethod
    def from_file(cls, filename, format=None):
        if os.path.getsize(filename) == 0:
            raise RuntimeError('%s is empty' % filename)

        def gen():
            with open_file(filename, format) as f:
                for line in f:
                    yield line

        ret = cls()
        ret.generator = gen
        ret.data_shapes = []
        ret.data_types = str
        return ret

    @classmethod
    def from_record_file(cls, filename):
        if os.path.getsize(filename) == 0:
            raise RuntimeError('%s is empty' % filename)
        gen = open_record(filename)
        ret = cls()
        ret.generator = gen
        ret.data_shapes = []
        ret.data_types = str
        return ret

    @classmethod
    def from_list(cls, ls):
        if not isinstance(ls, list):
            raise ValueError('expect list, got %s' % repr(ls))

        def gen():
            for i in ls:
                yield i

        ret = cls()
        ret.generator = gen
        ret.data_shapes = []
        ret.data_types = str
        return ret

    def __init__(self):
        self.name = None
        self._data_shapes = None
        self._data_types = None
        self.generator = None
        self.pyreader = None

    def __repr__(self):
        return 'Dataset: name: %s, data_shapes %s, data_types %s' % (
            self.name, self._data_shapes, self._data_types)

    def __eq__(self, other):
        return self.name == other.name and \
               self._data_shapes == other._data_shapes and \
               self._data_types == other._data_types

    def __iter__(self):
        return self.generator()

    #def __call__(self):
    #    return self.generator()

    def _infer_shapes_and_types(self):
        if self.generator is not None and self.name is not None:
            log.info('Try to infer data shapes & types from generator')
            first_value = next(self.generator())
            shapes, types = [], []
            for v in first_value:
                if not isinstance(v, np.ndarray):
                    raise ValueError(
                        'dataset generator should use numpy elements, got %s' %
                        first_value)
                shapes.append(v.shape)
                types.append(v.dtype.name)
            self._data_shapes = shapes
            self._data_types = types
            log.info('Dataset `%s` has data_shapes: %s data_types: %s' %
                     (self.name, repr(shapes), repr(types)))
        else:
            raise ValueError(
                'Try to infer data shapes or types from incomplete Dataset')

    @property
    def data_shapes(self):
        if self._data_shapes is None:
            self._infer_shapes_and_types()
            return self._data_shapes
        else:
            return self._data_shapes

    @data_shapes.setter
    def data_shapes(self, val):
        self._data_shapes = val

    @property
    def data_types(self):
        if self._data_types is None:
            self._infer_shapes_and_types()
            return self._data_types
        else:
            return self._data_types

    @data_types.setter
    def data_types(self, val):
        self._data_types = val

    def apply(self, transform_func):
        #input_shapes = transform_func.input_shapes
        #input_types = transform_func.input_types
        #data_shapes = transform_func.data_shapes
        #data_types = transform_func.data_types
        #assert input_shapes == self._data_shapes
        #assert input_types = self._data_types
        ret_gen = transform_func(self.generator)
        ret = type(self).from_generator_func(ret_gen)
        if self.name is not None:
            ret.name = self.name
        #ret.data_shapes = data_shapes
        #ret.data_types = data_types
        return ret

    def shuffle(self, buffer_size):
        func = functools.partial(shuffle_func, buffer_size=buffer_size)
        return self.apply(func)

    def repeat(self, n=-1):
        func = functools.partial(repeat_func, n=n)
        return self.apply(func)

    def map(self, fn):
        func = functools.partial(map_func, fn=fn)
        return self.apply(func)

    def filter(self, fn):
        func = functools.partial(filter_func, fn=fn)
        return self.apply(func)

    def shard(self, num_shards, index):
        func = functools.partial(
            shard_func, num_shards=num_shards, index=index)
        return self.apply(func)

    def interleave(self, map_fn, cycle_length, block_length):
        func = functools.partial(
            interleave_func,
            map_fn=map_fn,
            cycle_length=cycle_length,
            block_length=block_length)
        return self.apply(func)

    def padded_batch(self, batch_size, pad_value=0, max_seqlen=None):
        func = functools.partial(
            padded_batch_func,
            batch_size=batch_size,
            pad_value=pad_value,
            max_seqlen=max_seqlen)
        return self.apply(func)

    def take(self, count=1):
        func = functools.partial(take_func, count=count)
        return self.apply(func)

    def buffered(self, size=10):
        func = functools.partial(buffered_func, size=size)
        return self.apply(func)
