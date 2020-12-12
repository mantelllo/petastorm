from decimal import Decimal
from functools import partial
from packaging import version

import numpy as np
import pyarrow as pa
import pytest
# Must import pyarrow before torch. See: https://github.com/uber/petastorm/blob/master/docs/troubleshoot.rst
import torch

from petastorm import make_reader, TransformSpec, make_batch_reader
from petastorm.pytorch import _sanitize_pytorch_types, DataLoader, BatchedDataLoader, decimal_friendly_collate
from petastorm.tests.test_common import TestSchema

ALL_DATA_LOADERS = [DataLoader, BatchedDataLoader]

BATCHABLE_FIELDS = set(TestSchema.fields.values()) - \
    {TestSchema.matrix_nullable, TestSchema.string_array_nullable,
     TestSchema.matrix_string, TestSchema.empty_matrix_string, TestSchema.integer_nullable}

TORCH_BATCHABLE_FIELDS = BATCHABLE_FIELDS - \
    {TestSchema.decimal, TestSchema.partition_key, }

# pylint: disable=unnecessary-lambda
MINIMAL_READER_FLAVOR_FACTORIES = [
    lambda url, **kwargs: make_reader(url, reader_pool_type='dummy', **kwargs),
]

# pylint: disable=unnecessary-lambda
ALL_READER_FLAVOR_FACTORIES = MINIMAL_READER_FLAVOR_FACTORIES + [
    lambda url, **kwargs: make_reader(url, reader_pool_type='thread', **kwargs),
    lambda url, **kwargs: make_reader(url, reader_pool_type='process', workers_count=1, **kwargs),
]


def _check_simple_reader(loader, expected_data, expected_fields):
    # Read a bunch of entries from the dataset and compare the data to reference
    def _type(v):
        return v.dtype if isinstance(v, np.ndarray) else type(v)

    def _unbatch(x):
        if isinstance(x, torch.Tensor):
            x_numpy = x.numpy()
            assert x_numpy.shape[0] == 1
            return x_numpy.squeeze(0)
        elif isinstance(x, list):
            return x[0]
        else:
            raise RuntimeError('Unexpected type while unbatching.')

    expected_field_names = [f.name for f in expected_fields]
    for actual in loader:
        actual_numpy = {k: _unbatch(v) for k, v in actual.items() if k in expected_field_names}
        expected_all_fields = next(d for d in expected_data if d['id'] == actual_numpy['id'])
        expected = {k: v for k, v in expected_all_fields.items() if k in expected_field_names}
        np.testing.assert_equal(actual_numpy, expected)
        actual_types = [_type(v) for v in actual_numpy.values()]
        expected_types = [_type(v) for v in actual_numpy.values()]
        assert actual_types == expected_types


def _sensor_name_to_int(row):
    result_row = dict(**row)
    result_row['sensor_name'] = 0
    return result_row


@pytest.mark.parametrize('reader_factory', ALL_READER_FLAVOR_FACTORIES)
def test_simple_read(synthetic_dataset, reader_factory):
    with DataLoader(reader_factory(synthetic_dataset.url, schema_fields=BATCHABLE_FIELDS,
                                   transform_spec=TransformSpec(_sensor_name_to_int))) as loader:
        _check_simple_reader(loader, synthetic_dataset.data, BATCHABLE_FIELDS - {TestSchema.sensor_name})


@pytest.mark.parametrize('reader_factory', ALL_READER_FLAVOR_FACTORIES)
def test_simple_read_batched(synthetic_dataset, reader_factory):
    with BatchedDataLoader(reader_factory(synthetic_dataset.url, schema_fields=TORCH_BATCHABLE_FIELDS,
                                          transform_spec=TransformSpec(_sensor_name_to_int))) as loader:
        _check_simple_reader(loader, synthetic_dataset.data, TORCH_BATCHABLE_FIELDS - {TestSchema.sensor_name})


def test_sanitize_pytorch_types_int8():
    _TORCH_BEFORE_1_1 = version.parse(torch.__version__) < version.parse('1.1.0')

    dict_to_sanitize = {'a': np.asarray([-1, 1], dtype=np.int8)}
    _sanitize_pytorch_types(dict_to_sanitize)

    np.testing.assert_array_equal(dict_to_sanitize['a'], [-1, 1])
    if _TORCH_BEFORE_1_1:
        assert dict_to_sanitize['a'].dtype == np.int16
    else:
        assert dict_to_sanitize['a'].dtype == np.int8


def test_decimal_friendly_collate_empty_input():
    assert decimal_friendly_collate([dict()]) == dict()


@pytest.mark.parametrize('numpy_dtype',
                         [np.int8, np.uint8, np.int16, np.uint16, np.int32, np.uint32, np.int64])
def test_torch_tensorable_types(numpy_dtype):
    """Make sure that we 'sanitize' only integer types that can not be made into torch tensors natively"""
    value = np.zeros((2, 2), dtype=numpy_dtype)
    dict_to_sanitize = {'value': value}
    _sanitize_pytorch_types(dict_to_sanitize)

    torchable = False
    try:
        torch.Tensor(value)
        torchable = True
    except TypeError:
        pass

    tensor = torch.as_tensor(dict_to_sanitize['value'])

    tensor_and_back = tensor.numpy()

    if tensor_and_back.dtype != value.dtype:
        assert tensor_and_back.dtype.itemsize > value.dtype.itemsize
        assert not torchable, '_sanitize_pytorch_types modified value of type {}, but it was possible to create a ' \
                              'Tensor directly from a value with that type'.format(numpy_dtype)


def test_decimal_friendly_collate_input_has_decimals_in_dictionary():
    desired = {
        'decimal': [Decimal('1.0'), Decimal('1.1')],
        'int': [1, 2]
    }
    input_batch = [
        {'decimal': Decimal('1.0'), 'int': 1},
        {'decimal': Decimal('1.1'), 'int': 2},
    ]
    actual = decimal_friendly_collate(input_batch)

    assert len(actual) == 2
    assert desired['decimal'] == actual['decimal']
    np.testing.assert_equal(desired['int'], actual['int'].numpy())


def test_decimal_friendly_collate_input_has_decimals_in_tuple():
    input_batch = ([Decimal('1.0'), 1], [Decimal('1.1'), 2])
    desired = [(Decimal('1.0'), Decimal('1.1')), (1, 2)]
    actual = decimal_friendly_collate(input_batch)

    assert len(actual) == 2
    assert desired[0] == actual[0]
    np.testing.assert_equal(desired[1], actual[1].numpy())


@pytest.mark.parametrize('reader_factory', MINIMAL_READER_FLAVOR_FACTORIES)
@pytest.mark.parametrize("data_loader_type", ALL_DATA_LOADERS)
def test_no_shuffling(synthetic_dataset, reader_factory, data_loader_type):
    with data_loader_type(reader_factory(synthetic_dataset.url, schema_fields=['^id$'], workers_count=1,
                                         shuffle_row_groups=False)) as loader:
        ids = [row['id'][0].numpy() for row in loader]
        # expected_ids would be [0, 1, 2, ...]
        expected_ids = [row['id'] for row in synthetic_dataset.data]
        np.testing.assert_array_equal(expected_ids, ids)


@pytest.mark.parametrize('reader_factory', MINIMAL_READER_FLAVOR_FACTORIES)
@pytest.mark.parametrize("data_loader_type", ALL_DATA_LOADERS)
def test_with_shuffling_buffer(synthetic_dataset, reader_factory, data_loader_type):
    with data_loader_type(reader_factory(synthetic_dataset.url, schema_fields=['^id$'], workers_count=1,
                                         shuffle_row_groups=False),
                          shuffling_queue_capacity=51) as loader:
        ids = [row['id'][0].numpy() for row in loader]

        assert len(ids) == len(synthetic_dataset.data), 'All samples should be returned after reshuffling'

        # diff(ids) would return all-'1' for the seqeunce (note that we used shuffle_row_groups=False)
        # We assume we get less then 10% of consequent elements for the sake of the test (this probability is very
        # close to zero)
        assert sum(np.diff(ids) == 1) < len(synthetic_dataset.data) / 10.0


@pytest.mark.parametrize('shuffling_queue_capacity', [0, 3, 11, 1000])
@pytest.mark.parametrize("data_loader_type", ALL_DATA_LOADERS)
def test_with_batch_reader(scalar_dataset, shuffling_queue_capacity, data_loader_type):
    """See if we are getting correct batch sizes when using DataLoader with make_batch_reader"""
    pytorch_compatible_fields = [k for k, v in scalar_dataset.data[0].items()
                                 if not isinstance(v, (np.datetime64, np.unicode_))]
    with data_loader_type(make_batch_reader(scalar_dataset.url, schema_fields=pytorch_compatible_fields),
                          batch_size=3, shuffling_queue_capacity=shuffling_queue_capacity) as loader:
        batches = list(loader)
        assert len(scalar_dataset.data) == sum(batch['id'].shape[0] for batch in batches)

        # list types are broken in pyarrow 0.15.0. Don't test list-of-int field
        if pa.__version__ != '0.15.0':
            assert len(scalar_dataset.data) == sum(batch['int_fixed_size_list'].shape[0] for batch in batches)
            assert batches[0]['int_fixed_size_list'].shape[1] == len(scalar_dataset.data[0]['int_fixed_size_list'])


@pytest.mark.parametrize('reader_factory', ALL_READER_FLAVOR_FACTORIES)
@pytest.mark.parametrize("data_loader_type", ALL_DATA_LOADERS)
def test_call_iter_on_dataloader_multiple_times(synthetic_dataset, reader_factory, data_loader_type):
    with data_loader_type(reader_factory(synthetic_dataset.url, schema_fields=[TestSchema.id]),
                          batch_size=2) as loader:
        pass1_set = set()
        for batch in iter(loader):
            pass1_set |= set(batch['id'].numpy())
        pass2_set = set()
        for batch in iter(loader):
            pass2_set |= set(batch['id'].numpy())
        assert pass1_set == pass2_set
        match_str = 'You must finish a full pass of Petastorm DataLoader before making another pass from the beginning'
        with pytest.raises(RuntimeError, match=match_str):
            iter3 = iter(loader)
            next(iter3)
            iter4 = iter(loader)
            next(iter4)


def test_mem_cache_reader_num_epochs_error(two_columns_non_petastorm_dataset):
    error_string = "reader.num_epochs is currently 2. When cache in memory is "
    with make_batch_reader(two_columns_non_petastorm_dataset.url,
                           num_epochs=2) as reader:
        with pytest.raises(ValueError, match=error_string):
            BatchedDataLoader(reader, inmemory_cache_all=True)


def test_mem_cache_num_epochs_without_mem_cache_error(two_columns_non_petastorm_dataset):
    error_string = "num_epochs should not be specified when inmemory_cache_all is not enabled."
    with make_batch_reader(two_columns_non_petastorm_dataset.url,
                           num_epochs=1) as reader:
        with pytest.raises(ValueError, match=error_string):
            BatchedDataLoader(reader, num_epochs=2)


@pytest.mark.parametrize('shuffling_queue_capacity', [20, 0])
@pytest.mark.parametrize('reader_factory', [make_batch_reader, make_reader])
@pytest.mark.parametrize('num_epochs', [1, 2, 3, None])
def test_batched_data_loader_with_in_memory_cache(two_columns_non_petastorm_dataset,
                                                  shuffling_queue_capacity,
                                                  reader_factory,
                                                  num_epochs):
    batch_size = 10
    extra_loader_params = dict(inmemory_cache_all=True, num_epochs=num_epochs)
    extra_reader_params = dict(num_epochs=1)

    with reader_factory(two_columns_non_petastorm_dataset.url,
                        cur_shard=0,
                        shard_count=1,
                        reader_pool_type='thread',
                        workers_count=2,
                        hdfs_driver='libhdfs',
                        schema_fields=['col_0'], **extra_reader_params) as reader:

        loader = BatchedDataLoader(reader,
                                   batch_size=batch_size,
                                   transform_fn=partial(torch.as_tensor, device='cpu'),
                                   **extra_loader_params)

        it = iter(loader)
        retrieved_so_far = None
        for idx in range(5):
            batch = next(it)
            if idx == 0:
                first_buffer = loader._shuffling_buffer

            this_batch = batch['col_0'].clone()
            assert list(this_batch.shape)[0] == batch_size

            if retrieved_so_far is None:
                retrieved_so_far = this_batch
            else:
                intersect = set(retrieved_so_far.tolist()).intersection(set(this_batch.tolist()))
                assert not intersect
                retrieved_so_far = torch.cat([retrieved_so_far, this_batch], 0)

        retrieved_in_first_epoch = retrieved_so_far.clone()

        assert len(set(retrieved_so_far.tolist())) == 50

        if num_epochs == 1:
            with pytest.raises(StopIteration):
                next(it)

        if num_epochs in [2, 3, None]:
            for idx in range(5):
                batch = next(it)
                # Assert that a new buffer is created inside the loader
                assert loader._shuffling_buffer != first_buffer
                this_batch = batch['col_0'].clone()
                assert list(this_batch.shape)[0] == batch_size
                intersection = set(this_batch.tolist()).intersection(set(retrieved_in_first_epoch.tolist()))
                assert len(intersection) == batch_size
                retrieved_so_far = torch.cat([retrieved_so_far, this_batch], 0)

        if num_epochs == 2:
            with pytest.raises(StopIteration):
                next(it)

        if num_epochs in [3, None]:
            for idx in range(5):
                batch = next(it)
                this_batch = batch['col_0'].clone()
                assert list(this_batch.shape)[0] == batch_size
                retrieved_so_far = torch.cat([retrieved_so_far, this_batch], 0)

        if num_epochs == 3:
            with pytest.raises(StopIteration):
                next(it)

        if num_epochs is None:
            for idx in range(5):
                batch = next(it)
                this_batch = batch['col_0'].clone()
                assert list(this_batch.shape)[0] == batch_size
                retrieved_so_far = torch.cat([retrieved_so_far, this_batch], 0)
