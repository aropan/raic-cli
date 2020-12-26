"""Helpers methods for interacting with python fire."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
import inspect


def only_allow_defined_args(function_to_decorate):
  """Decorator which only allows arguments defined to be used.

  Note, we need to specify this, as Fire allows method chaining. This means
  that extra kwargs are kept around and passed to future methods that are
  called. We don't need this, and should fail early if this happens.

  Args:
    function_to_decorate: Function which to decorate.

  Returns:
    Wrapped function.
  """

  @functools.wraps(function_to_decorate)
  def _return_wrapped(*args, **kwargs):
    """Internal wrapper function."""
    valid_names, _, _, _ = inspect.getargspec(function_to_decorate)
    if "self" in valid_names:
      valid_names.remove("self")
    for arg_name in kwargs:
      if arg_name not in valid_names:
        raise ValueError("Unknown argument seen '%s', expected: [%s]" %
                         (arg_name, ", ".join(valid_names)))
    return function_to_decorate(*args, **kwargs)

  return _return_wrapped
