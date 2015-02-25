# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import itertools
import os
import shutil
import sys
import uuid
from collections import defaultdict

from twitter.common.collections import OrderedSet

from pants.backend.jvm.tasks.jvm_compile.jvm_dependency_analyzer import JvmDependencyAnalyzer
from pants.backend.jvm.tasks.jvm_compile.jvm_fingerprint_strategy import JvmFingerprintStrategy
from pants.backend.jvm.tasks.jvm_compile.resource_mapping import ResourceMapping
from pants.base.build_environment import get_buildroot, get_scm
from pants.base.exceptions import TaskError
from pants.base.target import Target
from pants.base.worker_pool import Work
from pants.goal.products import MultipleRootedProducts
from pants.option.options import Options
from pants.reporting.reporting_utils import items_to_report_element
from pants.util.contextutil import open_zip64, temporary_dir
from pants.util.dirutil import safe_mkdir, safe_rmtree, safe_walk


class JvmCompileStrategy(object):
  """A strategy for JVM compilation that uses a global classpath and analysis."""

  def __init__(self, context, options, workdir, analysis_tools):
    self.context = context

    # Various working directories.
    self._analysis_dir = os.path.join(workdir, 'analysis')
    self._classes_dir = os.path.join(workdir, 'classes')
    self._resources_dir = os.path.join(workdir, 'resources')

    self._delete_scratch = options.delete_scratch

    self._analysis_file = os.path.join(self._analysis_dir, 'global_analysis.valid')
    self._invalid_analysis_file = os.path.join(self._analysis_dir, 'global_analysis.invalid')

    # Language specific analysis tools.
    self._analysis_tools = analysis_tools

    # A temporary, but well-known, dir in which to munge analysis/dependency files in before
    # caching. It must be well-known so we know where to find the files when we retrieve them from
    # the cache.
    self._analysis_tmpdir = os.path.join(self._analysis_dir, 'artifact_cache_tmpdir')

    # The rough number of source files to build in each compiler pass.
    self._partition_size_hint = options.partition_size_hint

    # The ivy confs for which we're building.
    self._confs = options.confs

    # Set up dep checking if needed.
    def munge_flag(flag):
      flag_value = getattr(options, flag, None)
      return None if flag_value == 'off' else flag_value

    check_missing_deps = munge_flag('missing_deps')
    check_missing_direct_deps = munge_flag('missing_direct_deps')
    check_unnecessary_deps = munge_flag('unnecessary_deps')

    if check_missing_deps or check_missing_direct_deps or check_unnecessary_deps:
      target_whitelist = options.missing_deps_whitelist
      # Must init it here, so it can set requirements on the context.
      self._dep_analyzer = JvmDependencyAnalyzer(self.context,
                                                 check_missing_deps,
                                                 check_missing_direct_deps,
                                                 check_unnecessary_deps,
                                                 target_whitelist)
    else:
      self._dep_analyzer = None

    # If non-zero, and we have fewer than this number of locally-changed targets,
    # then we partition them separately, to preserve stability in the face of repeated
    # compilations.
    self._changed_targets_heuristic_limit = options.changed_targets_heuristic_limit

    # Sources (relative to buildroot) present in the last analysis that have since been deleted.
    # Populated in prepare_compile().
    self._deleted_sources = None

  def move(self, src, dst):
    if self._delete_scratch:
      shutil.move(src, dst)
    else:
      shutil.copy(src, dst)

  def pre_compile(self):
    # Only create these working dirs during execution phase, otherwise, they
    # would be wiped out by clean-all goal/task if it's specified.
    safe_mkdir(self._classes_dir)
    safe_mkdir(self._analysis_dir)

  def prepare_compile(self, cache_manager, sources_by_target, all_targets):
    """Prepares to compile the given set of targets.
    
    Has the side effects of pruning old analysis, and computing deleted sources.
    """
    # Update the classpath for us and for downstream tasks.
    compile_classpaths = self.context.products.get_data('compile_classpath')
    for conf in self._confs:
      compile_classpaths.add_for_targets(all_targets, [(conf, self._classes_dir), (conf, self._resources_dir)])

    # Split the global analysis file into valid and invalid parts.
    invalidation_check = cache_manager.check(all_targets)
    if invalidation_check.invalid_vts:
      # The analysis for invalid and deleted sources is no longer valid.
      invalid_targets = [vt.target for vt in invalidation_check.invalid_vts]
      invalid_sources_by_target = {}
      for tgt in invalid_targets:
        invalid_sources_by_target[tgt] = sources_by_target[tgt]
      invalid_sources = list(itertools.chain.from_iterable(invalid_sources_by_target.values()))
      self._deleted_sources = self._compute_deleted_sources()

      self._ensure_analysis_tmpdir()
      tmpdir = os.path.join(self._analysis_tmpdir, str(uuid.uuid4()))
      os.mkdir(tmpdir)
      valid_analysis_tmp = os.path.join(tmpdir, 'valid_analysis')
      newly_invalid_analysis_tmp = os.path.join(tmpdir, 'newly_invalid_analysis')
      invalid_analysis_tmp = os.path.join(tmpdir, 'invalid_analysis')
      if self._analysis_parser.is_nonempty_analysis(self._analysis_file):
        with self.context.new_workunit(name='prepare-analysis'):
          self._analysis_tools.split_to_paths(self._analysis_file,
              [(invalid_sources + self._deleted_sources, newly_invalid_analysis_tmp)],
              valid_analysis_tmp)
          if self._analysis_parser.is_nonempty_analysis(self._invalid_analysis_file):
            self._analysis_tools.merge_from_paths(
              [self._invalid_analysis_file, newly_invalid_analysis_tmp], invalid_analysis_tmp)
          else:
            invalid_analysis_tmp = newly_invalid_analysis_tmp

          # Now it's OK to overwrite the main analysis files with the new state.
          self.move(valid_analysis_tmp, self._analysis_file)
          self.move(invalid_analysis_tmp, self._invalid_analysis_file)
    else:
      self._deleted_sources = []

  def invalidation_hints(self, sources_by_target):
    """A tuple of partition_size_hint and locally_changed targets for the given inputs."""
    # If needed, find targets that we've changed locally (as opposed to
    # changes synced in from the SCM).
    # TODO(benjy): Should locally_changed_targets be available in all Tasks?
    locally_changed_targets = None
    if self._changed_targets_heuristic_limit:
      locally_changed_targets = self._find_locally_changed_targets(sources_by_target)
      if (locally_changed_targets and
          len(locally_changed_targets) > self._changed_targets_heuristic_limit):
        locally_changed_targets = None

    return (self._partition_size_hint, locally_changed_targets)

  def compile_chunk(self,
                    invalidation_check,
                    sources_by_target,
                    relevant_targets,
                    invalid_targets,
                    extra_compile_time_classpath_elements,
                    compile_vts):
    """A generator that returns compilation tuples for invalid targets in a single chunk.
    
    Has the side effects of populating:
    # valid/invalid analysis files
    # classes_by_source product
    # classes_by_target product
    # resources_by_target product
    """
    # Get the classpath generated by upstream JVM tasks and our own prepare_compile().
    compile_classpaths = self.context.products.get_data('compile_classpath')

    # Add any extra compile-time-only classpath elements.
    # TODO(benjy): Model compile-time vs. runtime classpaths more explicitly.
    # TODO(benjy): Add a pre-execute goal for injecting deps into targets, so e.g.,
    # we can inject a dep on the scala runtime library and still have it ivy-resolve.
    def extra_compile_classpath_iter():
      for conf in self._confs:
        for jar in extra_compile_time_classpath_elements:
          yield (conf, jar)
    compile_classpath = compile_classpaths.get_for_targets(relevant_targets)
    compile_classpath = OrderedSet(list(extra_compile_classpath_iter()) + list(compile_classpath))

    # Validate that all classpath entries are located within the working copy, which
    # simplifies relativizing the analysis files.
    self._validate_classpath(compile_classpath)

    # Find the invalid sources for this chunk.
    invalid_sources_by_target = {k:v for (k,v) in sources_by_target.items() if k in invalid_targets}

    tmpdir = os.path.join(self._analysis_tmpdir, str(uuid.uuid4()))
    os.mkdir(tmpdir)

    # Figure out the sources and analysis belonging to each partition.
    partitions = []  # Each element is a triple (vts, sources_by_target, analysis).
    for vts in invalidation_check.invalid_vts_partitioned:
      partition_tmpdir = os.path.join(tmpdir, Target.maybe_readable_identify(vts.targets))
      os.mkdir(partition_tmpdir)
      sources = list(itertools.chain.from_iterable(
          [invalid_sources_by_target.get(t, []) for t in vts.targets]))
      de_duped_sources = list(OrderedSet(sources))
      if len(sources) != len(de_duped_sources):
        counts = [(src, len(list(srcs))) for src, srcs in itertools.groupby(sorted(sources))]
        self.context.log.warn(
            'De-duped the following sources:\n\t%s' %
            '\n\t'.join(sorted('%d %s' % (cnt, src) for src, cnt in counts if cnt > 1)))
      analysis_file = os.path.join(partition_tmpdir, 'analysis')
      partitions.append((vts, de_duped_sources, analysis_file))

    # Split per-partition files out of the global invalid analysis.
    if self._analysis_parser.is_nonempty_analysis(self._invalid_analysis_file) and partitions:
      with self.context.new_workunit(name='partition-analysis'):
        splits = [(x[1], x[2]) for x in partitions]
        # We have to pass the analysis for any deleted files through zinc, to give it
        # a chance to delete the relevant class files.
        if splits:
          splits[0] = (splits[0][0] + self._deleted_sources, splits[0][1])
        self._analysis_tools.split_to_paths(self._invalid_analysis_file, splits)

    # Now compile partitions one by one.
    for partition_index, partition in enumerate(partitions):
      (vts, sources, analysis_file) = partition
      cp_entries = [entry for conf, entry in compile_classpath if conf in self._confs]

      progress_message = 'partition {} of {}'.format(partition_index + 1, len(partitions))
      compile_vts(vts, sources, analysis_file, cp_entries, self._classes_dir, progress_message)

      # No exception was thrown, therefore the compile succeeded and analysis_file is now valid.
      if os.path.exists(analysis_file):  # The compilation created an analysis.
        # Merge the newly-valid analysis with our global valid analysis.
        new_valid_analysis = analysis_file + '.valid.new'
        if self._analysis_parser.is_nonempty_analysis(self._analysis_file):
          with self.context.new_workunit(name='update-upstream-analysis'):
            self._analysis_tools.merge_from_paths([self._analysis_file, analysis_file],
                                                  new_valid_analysis)
        else:  # We need to keep analysis_file around. Background tasks may need it.
          shutil.copy(analysis_file, new_valid_analysis)

        # Move the merged valid analysis to its proper location.
        # We do this before checking for missing dependencies, so that we can still
        # enjoy an incremental compile after fixing missing deps.
        self.move(new_valid_analysis, self._analysis_file)

        # Update the products with the latest classes. Must happen before the
        # missing dependencies check.
        self._register_products(vts.targets, analysis_file)
        if self._dep_analyzer:
          # Check for missing dependencies.
          actual_deps = self._analysis_parser.parse_deps_from_path(analysis_file,
              lambda: self._compute_classpath_elements_by_class(cp_entries), self._classes_dir)
          with self.context.new_workunit(name='find-missing-dependencies'):
            self._dep_analyzer.check(sources, actual_deps)

        # Kick off the background artifact cache write.
        if self.artifact_cache_writes_enabled():
          self._write_to_artifact_cache(analysis_file, vts, invalid_sources_by_target)

      if self._analysis_parser.is_nonempty_analysis(self._invalid_analysis_file):
        with self.context.new_workunit(name='trim-downstream-analysis'):
          # Trim out the newly-valid sources from our global invalid analysis.
          new_invalid_analysis = analysis_file + '.invalid.new'
          discarded_invalid_analysis = analysis_file + '.invalid.discard'
          self._analysis_tools.split_to_paths(self._invalid_analysis_file,
            [(sources, discarded_invalid_analysis)], new_invalid_analysis)
          self.move(new_invalid_analysis, self._invalid_analysis_file)

      # Record the built target -> sources mapping for future use.
      for target in vts.targets:
        self._record_sources_by_target(target, sources_by_target.get(target, []))

      # Now that all the analysis accounting is complete, and we have no missing deps,
      # we can safely mark the targets as valid.
      vts.update()

  def _validate_classpath(self, files):
    """Validates that all files are located within the working copy, to simplify relativization."""
    buildroot = get_buildroot()
    for _,f in files:
      if os.path.relpath(f, buildroot).startswith('..'):
        raise TaskError('Classpath entry {f} is located outside the buildroot.'.format(f=f))

  def post_process_cached_vts(self, cached_vts):
    """Special post processing for global scala analysis files.
    
    Class files are retrieved directly into their final locations in the global classes dir.
    """
    self._ensure_analysis_tmpdir()

    # Get all the targets whose artifacts we found in the cache.
    cached_targets = []
    for vt in cached_vts:
      for target in vt.targets:
        cached_targets.append(target)

    # The current global analysis may contain old data for modified targets for
    # which we got cache hits. We need to strip out this old analysis, to ensure
    # that the new data incoming from the cache doesn't collide with it during the merge.
    sources_to_strip = []
    if os.path.exists(self._analysis_file):
      for target in cached_targets:
        sources_to_strip.extend(self._get_previous_sources_by_target(target))

    # Localize the cached analyses.
    analyses_to_merge = []
    for target in cached_targets:
      analysis_file = JvmCompileStrategy.analysis_for_target(self._analysis_tmpdir, target)
      portable_analysis_file = JvmCompileStrategy.portable_analysis_for_target(self._analysis_tmpdir,
                                                                        target)
      if os.path.exists(portable_analysis_file):
        self._analysis_tools.localize(portable_analysis_file, analysis_file)
      if os.path.exists(analysis_file):
        analyses_to_merge.append(analysis_file)

    # Merge them into the global analysis.
    if analyses_to_merge:
      with temporary_dir() as tmpdir:
        if sources_to_strip:
          throwaway = os.path.join(tmpdir, 'throwaway')
          trimmed_analysis = os.path.join(tmpdir, 'trimmed')
          self._analysis_tools.split_to_paths(self._analysis_file,
                                          [(sources_to_strip, throwaway)],
                                          trimmed_analysis)
        else:
          trimmed_analysis = self._analysis_file
        if os.path.exists(trimmed_analysis):
          analyses_to_merge.append(trimmed_analysis)
        tmp_analysis = os.path.join(tmpdir, 'analysis')
        with self.context.new_workunit(name='merge_analysis'):
          self._analysis_tools.merge_from_paths(analyses_to_merge, tmp_analysis)

        sources_by_cached_target = self._sources_for_targets(cached_targets)

        # Record the cached target -> sources mapping for future use.
        for target, sources in sources_by_cached_target.items():
          self._record_sources_by_target(target, sources)

        # Everything's good so move the merged analysis to its final location.
        if os.path.exists(tmp_analysis):
          self.move(tmp_analysis, self._analysis_file)

  def _write_to_artifact_cache(self, analysis_file, vts, sources_by_target):
    vt_by_target = dict([(vt.target, vt) for vt in vts.versioned_targets])

    vts_targets = [t for t in vts.targets if not t.has_label('no_cache')]

    split_analysis_files = [
        JvmCompileStrategy.analysis_for_target(self._analysis_tmpdir, t) for t in vts_targets]
    portable_split_analysis_files = [
        JvmCompileStrategy.portable_analysis_for_target(self._analysis_tmpdir, t) for t in vts_targets]

    # Set up args for splitting the analysis into per-target files.
    splits = zip([sources_by_target.get(t, []) for t in vts_targets], split_analysis_files)
    splits_args_tuples = [(analysis_file, splits)]

    # Set up args for rebasing the splits.
    relativize_args_tuples = zip(split_analysis_files, portable_split_analysis_files)

    # Set up args for artifact cache updating.
    vts_artifactfiles_pairs = []
    classes_by_source = self._compute_classes_by_source(analysis_file)
    resources_by_target = self.context.products.get_data('resources_by_target')
    for target, sources in sources_by_target.items():
      if target.has_label('no_cache'):
        continue
      artifacts = []
      if resources_by_target is not None:
        for _, paths in resources_by_target[target].abs_paths():
          artifacts.extend(paths)
      for source in sources:
        classes = classes_by_source.get(source, [])
        artifacts.extend(classes)

      vt = vt_by_target.get(target)
      if vt is not None:
        # NOTE: analysis_file doesn't exist yet.
        vts_artifactfiles_pairs.append(
            (vt,
             artifacts + [JvmCompileStrategy.portable_analysis_for_target(self._analysis_tmpdir, target)]))

    update_artifact_cache_work = self.get_update_artifact_cache_work(vts_artifactfiles_pairs)
    if update_artifact_cache_work:
      work_chain = [
        Work(self._analysis_tools.split_to_paths, splits_args_tuples, 'split'),
        Work(self._analysis_tools.relativize, relativize_args_tuples, 'relativize'),
        update_artifact_cache_work
      ]
      self.context.submit_background_work_chain(work_chain, parent_workunit_name='cache')

  def _compute_classes_by_source(self, analysis_file=None):
    """Compute src->classes.

    Srcs are relative to buildroot. Classes are absolute paths.
    """
    if analysis_file is None:
      analysis_file = self._analysis_file

    if not os.path.exists(analysis_file):
      return {}
    buildroot = get_buildroot()
    products = self._analysis_parser.parse_products_from_path(analysis_file, self._classes_dir)
    classes_by_src = {}
    for src, classes in products.items():
      relsrc = os.path.relpath(src, buildroot)
      classes_by_src[relsrc] = classes
    return classes_by_src

  def _compute_deleted_sources(self):
    """Computes the list of sources present in the last analysis that have since been deleted.

    This is a global list. We have no way of associating them to individual targets.
    Paths are relative to buildroot.
    """
    with self.context.new_workunit('find-deleted-sources'):
      if os.path.exists(self._analysis_file):
        products = self._analysis_parser.parse_products_from_path(self._analysis_file,
                                                                  self._classes_dir)
        buildroot = get_buildroot()
        old_srcs = products.keys()  # Absolute paths.
        return [os.path.relpath(src, buildroot) for src in old_srcs if not os.path.exists(src)]
      else:
        return []

  def _get_previous_sources_by_target(self, target):
    """Returns the target's sources as recorded on the last successful build of target.

    Returns a list of absolute paths.
    """
    path = os.path.join(self._target_sources_dir, target.identifier)
    if os.path.exists(path):
      with open(path, 'r') as infile:
        return [s.rstrip() for s in infile.readlines()]
    else:
      return []

  def _record_sources_by_target(self, target, sources):
    # Record target -> source mapping for future use.
    with open(os.path.join(self._target_sources_dir, target.identifier), 'w') as outfile:
      for src in sources:
        outfile.write(os.path.join(get_buildroot(), src))
        outfile.write('\n')

  def _find_locally_changed_targets(self, sources_by_target):
    """Finds the targets whose sources have been modified locally.

    Returns a list of targets, or None if no SCM is available.
    """
    # Compute the src->targets mapping. There should only be one target per source,
    # but that's not yet a hard requirement, so the value is a list of targets.
    # TODO(benjy): Might this inverse mapping be needed elsewhere too?
    targets_by_source = defaultdict(list)
    for tgt, srcs in sources_by_target.items():
      for src in srcs:
        targets_by_source[src].append(tgt)

    ret = OrderedSet()
    scm = get_scm()
    if not scm:
      return None
    changed_files = scm.changed_files(include_untracked=True, relative_to=get_buildroot())
    for f in changed_files:
      ret.update(targets_by_source.get(f, []))
    return list(ret)

  def _compute_classpath_elements_by_class(self, classpath):
    # Don't consider loose classes dirs in our classes dir. Those will be considered
    # separately, by looking at products.
    def non_product(path):
      return path != self._classes_dir

    if self._upstream_class_to_path is None:
      self._upstream_class_to_path = {}
      classpath_entries = filter(non_product, classpath)
      for cp_entry in self.find_all_bootstrap_jars() + classpath_entries:
        # Per the classloading spec, a 'jar' in this context can also be a .zip file.
        if os.path.isfile(cp_entry) and ((cp_entry.endswith('.jar') or cp_entry.endswith('.zip'))):
          with open_zip64(cp_entry, 'r') as jar:
            for cls in jar.namelist():
              # First jar with a given class wins, just like when classloading.
              if cls.endswith(b'.class') and not cls in self._upstream_class_to_path:
                self._upstream_class_to_path[cls] = cp_entry
        elif os.path.isdir(cp_entry):
          for dirpath, _, filenames in safe_walk(cp_entry, followlinks=True):
            for f in filter(lambda x: x.endswith('.class'), filenames):
              cls = os.path.relpath(os.path.join(dirpath, f), cp_entry)
              if not cls in self._upstream_class_to_path:
                self._upstream_class_to_path[cls] = os.path.join(dirpath, f)
    return self._upstream_class_to_path

  def find_all_bootstrap_jars(self):
    def get_path(key):
      return self.context.java_sysprops.get(key, '').split(':')

    def find_jars_in_dirs(dirs):
      ret = []
      for d in dirs:
        if os.path.isdir(d):
          ret.extend(filter(lambda s: s.endswith('.jar'), os.listdir(d)))
      return ret

    # Note: assumes HotSpot, or some JVM that supports sun.boot.class.path.
    # TODO: Support other JVMs? Not clear if there's a standard way to do so.
    # May include loose classes dirs.
    boot_classpath = get_path('sun.boot.class.path')

    # Note that per the specs, overrides and extensions must be in jars.
    # Loose class files will not be found by the JVM.
    override_jars = find_jars_in_dirs(get_path('java.endorsed.dirs'))
    extension_jars = find_jars_in_dirs(get_path('java.ext.dirs'))

    # Note that this order matters: it reflects the classloading order.
    bootstrap_jars = filter(os.path.isfile, override_jars + boot_classpath + extension_jars)
    return bootstrap_jars  # Technically, may include loose class dirs from boot_classpath.

  @property
  def _analysis_parser(self):
    return self._analysis_tools.parser

  # TODO: this is copy pasta between the strategy and jvm_compile.py
  def _sources_for_targets(self, targets):
    """Returns a map target->sources for the specified targets."""
    if self._sources_by_target is None:
      raise TaskError('self._sources_by_target not computed yet.')
    return dict((t, self._sources_by_target.get(t, [])) for t in targets)

  # Work in a tmpdir so we don't stomp the main analysis files on error.
  # The tmpdir is cleaned up in a shutdown hook, because background work
  # may need to access files we create there even after this method returns.
  def _ensure_analysis_tmpdir(self):
    # Do this lazily, so we don't trigger creation of a worker pool unless we need it.
    if not os.path.exists(self._analysis_tmpdir):
      os.makedirs(self._analysis_tmpdir)
      if self._delete_scratch:
        self.context.background_worker_pool().add_shutdown_hook(
            lambda: safe_rmtree(self._analysis_tmpdir))

  def _resources_by_class_file(self, class_file_name, resource_mapping):
    assert class_file_name.endswith(".class")
    assert class_file_name.startswith(self.workdir)
    class_file_name = class_file_name[len(self._classes_dir) + 1:-len(".class")]
    class_name = class_file_name.replace("/", ".")
    return resource_mapping.get(class_name, [])

  def register_products(self, targets):
    return self._register_products(targets, self._analysis_file)

  def _register_products(self, targets, analysis_file):
    classes_by_source = self.context.products.get_data('classes_by_source')
    classes_by_target = self.context.products.get_data('classes_by_target')
    resources_by_target = self.context.products.get_data('resources_by_target')

    if classes_by_source is not None or classes_by_target is not None:
      computed_classes_by_source = self._compute_classes_by_source(analysis_file)
      resource_mapping = ResourceMapping(self._classes_dir)
      for target in targets:
        target_products = classes_by_target[target] if classes_by_target is not None else None
        for source in self._sources_by_target.get(target, []):  # Source is relative to buildroot.
          classes = computed_classes_by_source.get(source, [])  # Classes are absolute paths.
          for cls in classes:
            resources = self._resources_by_class_file(cls, resource_mapping)
            resources_by_target[target].add_abs_paths(self._classes_dir, resources)

          if classes_by_target is not None:
            target_products.add_abs_paths(self._classes_dir, classes)
          if classes_by_source is not None:
            classes_by_source[source].add_abs_paths(self._classes_dir, classes)

    # TODO(pl): https://github.com/pantsbuild/pants/issues/206
    if resources_by_target is not None:
      for target in targets:
        target_resources = resources_by_target[target]
        for root, abs_paths in self.extra_products(target):
          target_resources.add_abs_paths(root, abs_paths)
