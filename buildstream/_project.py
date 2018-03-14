#!/usr/bin/env python3
#
#  Copyright (C) 2016-2018 Codethink Limited
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.	 See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library. If not, see <http://www.gnu.org/licenses/>.
#
#  Authors:
#        Tristan Van Berkom <tristan.vanberkom@codethink.co.uk>

import os
import multiprocessing  # for cpu_count()
from collections import Mapping
from pluginbase import PluginBase
from . import utils
from . import _cachekey
from . import _site
from . import _yaml
from ._profile import Topics, profile_start, profile_end
from ._exceptions import LoadError, LoadErrorReason
from ._options import OptionPool
from ._artifactcache import artifact_cache_specs_from_config_node
from ._elementfactory import ElementFactory
from ._sourcefactory import SourceFactory
from ._projectrefs import ProjectRefs, ProjectRefStorage
from ._workspaces import Workspaces


# The base BuildStream format version
#
# This version is bumped whenever enhancements are made
# to the `project.conf` format or the core element format.
#
BST_FORMAT_VERSION = 6
BST_WORKSPACE_FORMAT_VERSION = 1

# The separator we use for user specified aliases
_ALIAS_SEPARATOR = ':'


# HostMount()
#
# A simple object describing the behavior of
# a host mount.
#
class HostMount():

    def __init__(self, path, host_path=None, optional=False):

        # Support environment variable expansion in host mounts
        path = os.path.expandvars(path)
        if host_path is not None:
            host_path = os.path.expandvars(host_path)

        self.path = path              # Path inside the sandbox
        self.host_path = host_path    # Path on the host
        self.optional = optional      # Optional mounts do not incur warnings or errors

        if self.host_path is None:
            self.host_path = self.path


# Project()
#
# The Project Configuration
#
class Project():

    def __init__(self, directory, context, *, junction=None, cli_options=None):

        # The project name
        self.name = None

        # The project directory
        self.directory = os.path.abspath(directory)

        # Absolute path to where elements are loaded from within the project
        self.element_path = None

        self._context = context  # The invocation Context
        self._variables = {}     # The default variables overridden with project wide overrides
        self._environment = {}   # The base sandbox environment
        self._elements = {}      # Element specific configurations
        self._sources = {}       # Source specific configurations
        self._aliases = {}       # Aliases dictionary
        self._workspaces = None  # Workspaces
        self._plugin_source_origins = []   # Origins of custom sources
        self._plugin_element_origins = []  # Origins of custom elements
        self._options = None    # Project options, the OptionPool
        self._junction = junction   # The junction element, if this is a subproject
        self._cli_options = cli_options
        self._cache_key = None
        self._source_format_versions = {}
        self._element_format_versions = {}
        self._fail_on_overlap = False
        self._ref_storage = None  # The ProjectRefStorage setting

        # The project.refs management object for this project
        self.refs = ProjectRefs(self.directory)

        # Shell options
        self._shell_command = []      # The default interactive shell command
        self._shell_environment = {}  # Statically set environment vars
        self._shell_host_files = []   # A list of HostMount objects

        profile_start(Topics.LOAD_PROJECT, self.directory.replace(os.sep, '-'))
        self._load()
        profile_end(Topics.LOAD_PROJECT, self.directory.replace(os.sep, '-'))

        self._context._add_project(self)

    # translate_url():
    #
    # Translates the given url which may be specified with an alias
    # into a fully qualified url.
    #
    # Args:
    #    url (str): A url, which may be using an alias
    #
    # Returns:
    #    str: The fully qualified url, with aliases resolved
    #
    # This method is provided for :class:`.Source` objects to resolve
    # fully qualified urls based on the shorthand which is allowed
    # to be specified in the YAML
    def translate_url(self, url):
        if url and _ALIAS_SEPARATOR in url:
            url_alias, url_body = url.split(_ALIAS_SEPARATOR, 1)
            alias_url = self._aliases.get(url_alias)
            if alias_url:
                url = alias_url + url_body

        return url

    # _load():
    #
    # Loads the project configuration file in the project directory.
    #
    # Raises: LoadError if there was a problem with the project.conf
    #
    def _load(self):

        # Load builtin default
        projectfile = os.path.join(self.directory, "project.conf")
        config = _yaml.load(_site.default_project_config)

        # Load project local config and override the builtin
        project_conf = _yaml.load(projectfile)
        _yaml.composite(config, project_conf)

        # Element and Source  type configurations will be composited later onto
        # element/source types, so we delete it from here and run our final
        # assertion after.
        self._elements = _yaml.node_get(config, Mapping, 'elements', default_value={})
        self._sources = _yaml.node_get(config, Mapping, 'sources', default_value={})
        config.pop('elements', None)
        config.pop('sources', None)
        _yaml.node_final_assertions(config)
        _yaml.node_validate(config, [
            'format-version',
            'element-path', 'variables',
            'environment', 'environment-nocache',
            'split-rules', 'elements', 'plugins',
            'aliases', 'name',
            'artifacts', 'options',
            'fail-on-overlap', 'shell',
            'ref-storage', 'sandbox'
        ])

        # The project name, element path and option declarations
        # are constant and cannot be overridden by option conditional statements
        self.name = _yaml.node_get(config, str, 'name')
        self.element_path = os.path.join(
            self.directory,
            _yaml.node_get(config, str, 'element-path')
        )

        # Load project options
        options_node = _yaml.node_get(config, Mapping, 'options', default_value={})
        self._options = OptionPool(self.element_path)
        self._options.load(options_node)
        if self._junction:
            # load before user configuration
            self._options.load_yaml_values(self._junction.options, transform=self._junction._subst_string)

        # Collect option values specified in the user configuration
        overrides = self._context._get_overrides(self.name)
        override_options = _yaml.node_get(overrides, Mapping, 'options', default_value={})
        self._options.load_yaml_values(override_options)
        if self._cli_options:
            self._options.load_cli_values(self._cli_options)

        # We're done modifying options, now we can use them for substitutions
        self._options.resolve()

        #
        # Now resolve any conditionals in the remaining configuration,
        # any conditionals specified for project option declarations,
        # or conditionally specifying the project name; will be ignored.
        #
        self._options.process_node(config)

        #
        # Now all YAML composition is done, from here on we just load
        # the values from our loaded configuration dictionary.
        #

        # Load artifacts pull/push configuration for this project
        self.artifact_cache_specs = artifact_cache_specs_from_config_node(config)

        # Workspace configurations
        self._workspaces = Workspaces(self)

        # Assert project version
        format_version = _yaml.node_get(config, int, 'format-version')
        if BST_FORMAT_VERSION < format_version:
            major, minor = utils.get_bst_version()
            raise LoadError(
                LoadErrorReason.UNSUPPORTED_PROJECT,
                "Project requested format version {}, but BuildStream {}.{} only supports up until format version {}"
                .format(format_version, major, minor, BST_FORMAT_VERSION))

        # Plugin origins and versions
        origins = _yaml.node_get(config, list, 'plugins', default_value=[])
        for origin in origins:
            allowed_origin_fields = [
                'origin', 'sources', 'elements',
                'package-name', 'path',
            ]
            allowed_origins = ['core', 'local', 'pip']
            _yaml.node_validate(origin, allowed_origin_fields)

            if origin['origin'] not in allowed_origins:
                raise LoadError(
                    LoadErrorReason.INVALID_YAML,
                    "Origin '{}' is not one of the allowed types"
                    .format(origin['origin']))

            # Store source versions for checking later
            source_versions = _yaml.node_get(origin, Mapping, 'sources', default_value={})
            for key, _ in _yaml.node_items(source_versions):
                if key in self._source_format_versions:
                    raise LoadError(
                        LoadErrorReason.INVALID_YAML,
                        "Duplicate listing of source '{}'".format(key))
                self._source_format_versions[key] = _yaml.node_get(source_versions, int, key)

            # Store element versions for checking later
            element_versions = _yaml.node_get(origin, Mapping, 'elements', default_value={})
            for key, _ in _yaml.node_items(element_versions):
                if key in self._element_format_versions:
                    raise LoadError(
                        LoadErrorReason.INVALID_YAML,
                        "Duplicate listing of element '{}'".format(key))
                self._element_format_versions[key] = _yaml.node_get(element_versions, int, key)

            # Store the origins if they're not 'core'.
            # core elements are loaded by default, so storing is unnecessary.
            if _yaml.node_get(origin, str, 'origin') != 'core':
                self._store_origin(origin, 'sources', self._plugin_source_origins)
                self._store_origin(origin, 'elements', self._plugin_element_origins)

        pluginbase = PluginBase(package='buildstream.plugins')
        self._element_factory = ElementFactory(pluginbase, self._plugin_element_origins)
        self._source_factory = SourceFactory(pluginbase, self._plugin_source_origins)

        # Source url aliases
        self._aliases = _yaml.node_get(config, Mapping, 'aliases', default_value={})

        # Load base variables
        self._variables = _yaml.node_get(config, Mapping, 'variables')

        # Add the project name as a default variable
        self._variables['project-name'] = self.name

        # Extend variables with automatic variables and option exports
        # Initialize it as a string as all variables are processed as strings.
        self._variables['max-jobs'] = str(multiprocessing.cpu_count())

        # Export options into variables, if that was requested
        for _, option in self._options.options.items():
            if option.variable:
                self._variables[option.variable] = option.get_value()

        # Load sandbox environment variables
        self._environment = _yaml.node_get(config, Mapping, 'environment')
        self._env_nocache = _yaml.node_get(config, list, 'environment-nocache')

        # Load sandbox configuration
        self._sandbox = _yaml.node_get(config, Mapping, 'sandbox')

        # Load project split rules
        self._splits = _yaml.node_get(config, Mapping, 'split-rules')

        # Fail on overlap
        self._fail_on_overlap = _yaml.node_get(config, bool, 'fail-on-overlap')

        # Use separate file for storing source references
        self._ref_storage = _yaml.node_get(config, str, 'ref-storage')
        if self._ref_storage not in [ProjectRefStorage.INLINE, ProjectRefStorage.PROJECT_REFS]:
            p = _yaml.node_get_provenance(config, 'ref-storage')
            raise LoadError(LoadErrorReason.INVALID_DATA,
                            "{}: Invalid value '{}' specified for ref-storage"
                            .format(p, self._ref_storage))

        # Load project.refs if it exists, this may be ignored.
        if self._ref_storage == ProjectRefStorage.PROJECT_REFS:
            self.refs.load(self._options)

        # Parse shell options
        shell_options = _yaml.node_get(config, Mapping, 'shell')
        _yaml.node_validate(shell_options, ['command', 'environment', 'host-files'])
        self._shell_command = _yaml.node_get(shell_options, list, 'command')

        # Perform environment expansion right away
        shell_environment = _yaml.node_get(shell_options, Mapping, 'environment', default_value={})
        for key, _ in _yaml.node_items(shell_environment):
            value = _yaml.node_get(shell_environment, str, key)
            self._shell_environment[key] = os.path.expandvars(value)

        # Host files is parsed as a list for convenience
        host_files = _yaml.node_get(shell_options, list, 'host-files', default_value=[])
        for host_file in host_files:
            if isinstance(host_file, str):
                mount = HostMount(host_file)
            else:
                # Some validation
                index = host_files.index(host_file)
                host_file_desc = _yaml.node_get(shell_options, Mapping, 'host-files', indices=[index])
                _yaml.node_validate(host_file_desc, ['path', 'host_path', 'optional'])

                # Parse the host mount
                path = _yaml.node_get(host_file_desc, str, 'path')
                host_path = _yaml.node_get(host_file_desc, str, 'host_path', default_value='') or None
                optional = _yaml.node_get(host_file_desc, bool, 'optional', default_value=False)
                mount = HostMount(path, host_path, optional)

            self._shell_host_files.append(mount)

    # _store_origin()
    #
    # Helper function to store plugin origins
    #
    # Args:
    #    origin (dict) - a dictionary indicating the origin of a group of
    #                    plugins.
    #    plugin_group (str) - The name of the type of plugin that is being
    #                         loaded
    #    destination (list) - A list of dicts to store the origins in
    #
    # Raises:
    #    LoadError if 'origin' is an unexpected value
    def _store_origin(self, origin, plugin_group, destination):
        expected_groups = ['sources', 'elements']
        if plugin_group not in expected_groups:
            raise LoadError(LoadErrorReason.INVALID_DATA,
                            "Unexpected plugin group: {}, expecting {}"
                            .format(plugin_group, expected_groups))
        if plugin_group in origin:
            origin_dict = _yaml.node_copy(origin)
            plugins = _yaml.node_get(origin, Mapping, plugin_group, default_value={})
            origin_dict['plugins'] = [k for k, _ in _yaml.node_items(plugins)]
            for group in expected_groups:
                if group in origin_dict:
                    del origin_dict[group]
            if origin_dict['origin'] == 'local':
                # paths are passed in relative to the project, but must be absolute
                origin_dict['path'] = os.path.join(self.directory, origin_dict['path'])
            destination.append(origin_dict)

    def _extract_plugin_paths(self, node, name):
        if not node:
            return
        path_list = _yaml.node_get(node, list, name, default_value=[])
        for i in range(len(path_list)):
            path = _yaml.node_get(node, str, name, indices=[i])
            yield path

    # _get_cache_key():
    #
    # Returns the cache key, calculating it if necessary
    #
    # Returns:
    #    (str): A hex digest cache key for the Context
    #
    def _get_cache_key(self):
        if self._cache_key is None:

            # Anything that alters the build goes into the unique key
            # (currently nothing here)
            self._cache_key = _cachekey.generate_key({})

        return self._cache_key

    def _create_element(self, kind, artifacts, meta):
        return self._element_factory.create(kind, self._context, self, artifacts, meta)

    def _create_source(self, kind, meta):
        return self._source_factory.create(kind, self._context, self, meta)
