# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

java_thrift_library(
  name = 'bad-thrift-default',
  sources = ['bad.thrift'],
  # thrift_linter_strict not defined
)

java_thrift_library(
  name = 'bad-thrift-strict',
  sources = ['bad.thrift'],
  thrift_linter_strict=True,
)

java_thrift_library(
  name = 'bad-thrift-non-strict',
  sources = ['bad.thrift'],
  thrift_linter_strict=False,
)

java_thrift_library(
  name = 'good-thrift',
  sources = ['good.thrift'],
  thrift_linter_strict=True,
)
