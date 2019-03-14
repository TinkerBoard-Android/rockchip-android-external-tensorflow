# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""Python wrapper for prefetching_ops."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from tensorflow.python.data.ops import dataset_ops
from tensorflow.python.data.ops import iterator_ops
from tensorflow.python.eager import context
from tensorflow.python.eager import function
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_spec
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import functional_ops
from tensorflow.python.ops import gen_dataset_ops


class _PerDeviceGenerator(dataset_ops.DatasetV2):
  """A `dummy` generator dataset."""

  def __init__(self, shard_num, multi_device_iterator_resource, incarnation_id,
               source_device, element_structure):
    self._structure = element_structure

    multi_device_iterator_string_handle = (
        gen_dataset_ops.multi_device_iterator_to_string_handle(
            multi_device_iterator_resource))

    @function.defun()
    def _init_func():
      return multi_device_iterator_string_handle

    init_func_concrete = _init_func._get_concrete_function_internal()  # pylint: disable=protected-access

    @function.defun()
    def _remote_init_func():
      return functional_ops.remote_call(
          target=source_device,
          args=init_func_concrete.captured_inputs,
          Tout=[dtypes.string],
          f=init_func_concrete)

    self._init_func = _remote_init_func._get_concrete_function_internal()  # pylint: disable=protected-access
    self._init_captured_args = self._init_func.captured_inputs

    @function.defun(input_signature=[tensor_spec.TensorSpec([], dtypes.string)])
    def _next_func(string_handle):
      # pylint: disable=protected-access
      multi_device_iterator = (
          gen_dataset_ops.multi_device_iterator_from_string_handle(
              string_handle=string_handle,
              output_types=self._structure._flat_types,
              output_shapes=self._structure._flat_shapes))
      return gen_dataset_ops.multi_device_iterator_get_next_from_shard(
          multi_device_iterator=multi_device_iterator,
          shard_num=shard_num,
          incarnation_id=incarnation_id,
          output_types=self._structure._flat_types,
          output_shapes=self._structure._flat_shapes)

    next_func_concrete = _next_func._get_concrete_function_internal()  # pylint: disable=protected-access

    @function.defun_with_attributes(
        input_signature=[tensor_spec.TensorSpec([], dtypes.string)],
        attributes={"experimental_ints_on_device": True})
    def _remote_next_func(string_handle):
      return functional_ops.remote_call(
          target=source_device,
          args=[string_handle] + next_func_concrete.captured_inputs,
          Tout=self._structure._flat_types,  # pylint: disable=protected-access
          f=next_func_concrete)

    self._next_func = _remote_next_func._get_concrete_function_internal()  # pylint: disable=protected-access
    self._next_captured_args = self._next_func.captured_inputs

    @function.defun(input_signature=[tensor_spec.TensorSpec([], dtypes.string)])
    def _finalize_func(unused_string_handle):
      return array_ops.constant(0, dtypes.int64)

    finalize_func_concrete = _finalize_func._get_concrete_function_internal()  # pylint: disable=protected-access

    @function.defun(input_signature=[tensor_spec.TensorSpec([], dtypes.string)])
    def _remote_finalize_func(string_handle):
      return functional_ops.remote_call(
          target=source_device,
          args=[string_handle] + finalize_func_concrete.captured_inputs,
          Tout=[dtypes.int64],
          f=finalize_func_concrete)

    self._finalize_func = _remote_finalize_func._get_concrete_function_internal(  # pylint: disable=protected-access
    )
    self._finalize_captured_args = self._finalize_func.captured_inputs

    variant_tensor = gen_dataset_ops.generator_dataset(
        self._init_captured_args,
        self._next_captured_args,
        self._finalize_captured_args,
        init_func=self._init_func,
        next_func=self._next_func,
        finalize_func=self._finalize_func,
        **dataset_ops.flat_structure(self))
    super(_PerDeviceGenerator, self).__init__(variant_tensor)

  def _inputs(self):
    # TODO(b/116506223): Determine which datasets should be used as inputs here.
    return []

  @property
  def _element_structure(self):
    return self._structure


class MultiDeviceIterator(object):
  """An iterator over multiple devices.

  @compatibility(eager)
  MultiDeviceIterator isn't currently supported in Eager mode but support is
  coming soon.
  @end_compatibility
  """

  def __init__(self,
               dataset,
               devices,
               max_buffer_size=1,
               prefetch_buffer_size=1,
               source_device="/cpu:0"):
    """Constructs a MultiDeviceIterator.

    Args:
      dataset: The input dataset to be iterated over.
      devices: The list of devices to fetch data to.
      max_buffer_size: Maximum size of the host side per device buffer to keep.
      prefetch_buffer_size: if > 1, then we setup a buffer on each device
        to prefetch into.
      source_device: The host device to place the `dataset` on.

    Raises:
      RuntimeError: If run in Eager mode.
    """
    self._dataset = dataset._apply_options()  # pylint: disable=protected-access
    self._devices = devices
    self._source_device = source_device
    self._source_device_tensor = ops.convert_to_tensor(source_device)

    # Create the MultiDeviceIterator.
    with ops.device(self._source_device):
      # TODO(b/121378567): Get rid of this shared_name hack.
      shared_name = ""
      if context.executing_eagerly():
        # Ensure a unique name when eager execution is enabled to avoid spurious
        # sharing issues.
        shared_name += str(ops.uid())
      self._multi_device_iterator_resource = (
          gen_dataset_ops.multi_device_iterator(
              devices=self._devices,
              shared_name=shared_name,
              container="",
              **dataset_ops.flat_structure(dataset)))

      # The incarnation ID is used to ensure consistency between the per-device
      # iterators and the multi-device iterator.
      self._incarnation_id = gen_dataset_ops.multi_device_iterator_init(
          self._dataset._variant_tensor,  # pylint: disable=protected-access
          self._multi_device_iterator_resource,
          max_buffer_size=max_buffer_size)

    # TODO(rohanj): Explore the possibility of the MultiDeviceIterator to
    # initialize the device side of the pipeline. This would allow the
    # MultiDeviceIterator to choose, for example, to move some transformations
    # into the device side from its input. It might be useful in rewriting.
    # Create the per device iterators.
    self._device_iterators = []
    for i, device in enumerate(self._devices):
      with ops.device(device):
        ds = _PerDeviceGenerator(
            i, self._multi_device_iterator_resource, self._incarnation_id,
            self._source_device_tensor, dataset._element_structure)  # pylint: disable=protected-access
        if prefetch_buffer_size > 0:
          ds = ds.prefetch(prefetch_buffer_size)
        # TODO(jsimsa): Enable auto-tuning and optimizations when supported for
        # non-CPU devices.
        options = dataset_ops.Options()
        options.experimental_autotune = False
        options.experimental_optimization.apply_default_optimizations = False
        ds = ds.with_options(options)
        if context.executing_eagerly():
          self._device_iterators.append(dataset_ops.make_one_shot_iterator(ds))
        else:
          self._device_iterators.append(
              dataset_ops.make_initializable_iterator(ds))

    if not context.executing_eagerly():
      device_iterator_initializers = [
          iterator.initializer for iterator in self._device_iterators
      ]
      self._initializer = control_flow_ops.group(*device_iterator_initializers)

  def get_next(self, device=None):
    """Returns the next element given a `device`, else returns all in a list."""
    if device is not None:
      index = self._devices.index(device)
      return self._device_iterators[index].get_next()

    result = []
    for i, device in enumerate(self._devices):
      with ops.device(device):
        result.append(self._device_iterators[i].get_next())
    return result

  def get_next_as_optional(self):
    result = []
    for i, device in enumerate(self._devices):
      with ops.device(device):
        result.append(iterator_ops.get_next_as_optional(
            self._device_iterators[i]))
    return result

  @property
  def initializer(self):
    if context.executing_eagerly():
      return control_flow_ops.no_op()
    return self._initializer

  @property
  def output_types(self):
    return self._dataset.output_types

  @property
  def output_shapes(self):
    return self._dataset.output_shapes

  @property
  def output_classes(self):
    return self._dataset.output_classes