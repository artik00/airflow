#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Manages all providers."""
import fnmatch
import importlib
import json
import logging
import os
from collections import OrderedDict
from functools import wraps
from time import perf_counter
from typing import Any, Callable, Dict, List, NamedTuple, Set, TypeVar, cast

import jsonschema
from wtforms import BooleanField, Field, IntegerField, PasswordField, StringField

from airflow.utils import yaml
from airflow.utils.entry_points import entry_points_with_dist
from airflow.utils.log.logging_mixin import LoggingMixin
from airflow.utils.module_loading import import_string

try:
    import importlib.resources as importlib_resources
except ImportError:
    # Try back-ported to PY<37 `importlib_resources`.
    import importlib_resources

ALLOWED_FIELD_CLASSES = [IntegerField, PasswordField, StringField, BooleanField]

log = logging.getLogger(__name__)


def _create_provider_info_schema_validator():
    """Creates JSON schema validator from the provider_info.schema.json"""
    schema = json.loads(importlib_resources.read_text('airflow', 'provider_info.schema.json'))
    cls = jsonschema.validators.validator_for(schema)
    validator = cls(schema)
    return validator


def _create_customized_form_field_behaviours_schema_validator():
    """Creates JSON schema validator from the customized_form_field_behaviours.schema.json"""
    schema = json.loads(
        importlib_resources.read_text('airflow', 'customized_form_field_behaviours.schema.json')
    )
    cls = jsonschema.validators.validator_for(schema)
    validator = cls(schema)
    return validator


def _sanity_check(provider_package: str, class_name: str) -> bool:
    """
    Performs sanity check on provider classes.
    For apache-airflow providers - it checks if it starts with appropriate package. For all providers
    it tries to import the provider - checking that there are no exceptions during importing.
    """
    if provider_package.startswith("apache-airflow"):
        provider_path = provider_package[len("apache-") :].replace("-", ".")
        if not class_name.startswith(provider_path):
            log.warning(
                "Sanity check failed when importing '%s' from '%s' package. It should start with '%s'",
                class_name,
                provider_package,
                provider_path,
            )
            return False
    try:
        import_string(class_name)
    except Exception as e:
        log.warning(
            "Exception when importing '%s' from '%s' package: %s",
            class_name,
            provider_package,
            e,
        )
        return False
    return True


class ProviderInfo(NamedTuple):
    """Provider information"""

    version: str
    provider_info: Dict


class HookInfo(NamedTuple):
    """Hook information"""

    connection_class: str
    connection_id_attribute_name: str
    package_name: str
    hook_name: str


class ConnectionFormWidgetInfo(NamedTuple):
    """Connection Form Widget information"""

    connection_class: str
    package_name: str
    field: Field


T = TypeVar("T", bound=Callable)

logger = logging.getLogger(__name__)


# We want to have better control over initialization of parameters and be able to debug and test it
# So we add our own decorator
def provider_info_cache(cache_name: str) -> Callable[[T], T]:
    """
    Decorator factory that create decorator that caches initialization of provider's parameters
    :param cache_name: Name of the cache
    """

    def provider_info_cache_decorator(func: T):
        @wraps(func)
        def wrapped_function(*args, **kwargs):
            providers_manager_instance = args[0]
            if cache_name in providers_manager_instance._initialized_cache:
                return
            start_time = perf_counter()
            logger.debug("Initializing Providers Manager[%s]", cache_name)
            func(*args, **kwargs)
            providers_manager_instance._initialized_cache[cache_name] = True
            logger.debug(
                "Initialization of Providers Manager[%s] took %.2f seconds",
                cache_name,
                perf_counter() - start_time,
            )

        return cast(T, wrapped_function)

    return provider_info_cache_decorator


class ProvidersManager(LoggingMixin):
    """
    Manages all provider packages. This is a Singleton class. The first time it is
    instantiated, it discovers all available providers in installed packages and
    local source folders (if airflow is run from sources).
    """

    _instance = None
    resource_version = "0"

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initializes the manager."""
        self._initialized_cache: Dict[str, bool] = {}
        # Keeps dict of providers keyed by module name
        self._provider_dict: Dict[str, ProviderInfo] = {}
        # Keeps dict of hooks keyed by connection type
        self._hooks_dict: Dict[str, HookInfo] = {}
        # Keeps methods that should be used to add custom widgets tuple of keyed by name of the extra field
        self._connection_form_widgets: Dict[str, ConnectionFormWidgetInfo] = {}
        # Customizations for javascript fields are kept here
        self._field_behaviours: Dict[str, Dict] = {}
        self._extra_link_class_name_set: Set[str] = set()
        self._logging_class_name_set: Set[str] = set()
        self._secrets_backend_class_name_set: Set[str] = set()
        self._api_auth_backend_module_names: Set[str] = set()
        self._provider_schema_validator = _create_provider_info_schema_validator()
        self._customized_form_fields_schema_validator = (
            _create_customized_form_field_behaviours_schema_validator()
        )

    @provider_info_cache("list")
    def initialize_providers_list(self):
        """Lazy initialization of providers list."""
        # Local source folders are loaded first. They should take precedence over the package ones for
        # Development purpose. In production provider.yaml files are not present in the 'airflow" directory
        # So there is no risk we are going to override package provider accidentally. This can only happen
        # in case of local development
        self._discover_all_airflow_builtin_providers_from_local_sources()
        self._discover_all_providers_from_packages()
        self._provider_dict = OrderedDict(sorted(self._provider_dict.items()))

    @provider_info_cache("hooks")
    def initialize_providers_hooks(self):
        """Lazy initialization of providers hooks."""
        self.initialize_providers_list()
        self._discover_hooks()
        self._hooks_dict = OrderedDict(sorted(self._hooks_dict.items()))
        self._connection_form_widgets = OrderedDict(sorted(self._connection_form_widgets.items()))
        self._field_behaviours = OrderedDict(sorted(self._field_behaviours.items()))

    @provider_info_cache("extra_links")
    def initialize_providers_extra_links(self):
        """Lazy initialization of providers extra links."""
        self.initialize_providers_list()
        self._discover_extra_links()

    @provider_info_cache("logging")
    def initialize_providers_logging(self):
        """Lazy initialization of providers logging information."""
        self.initialize_providers_list()
        self._discover_logging()

    @provider_info_cache("secrets_backends")
    def initialize_providers_secrets_backends(self):
        """Lazy initialization of providers secrets_backends information."""
        self.initialize_providers_list()
        self._discover_secrets_backends()

    @provider_info_cache("auth_backends")
    def initialize_providers_auth_backends(self):
        """Lazy initialization of providers API auth_backends information."""
        self.initialize_providers_list()
        self._discover_auth_backends()

    def _discover_all_providers_from_packages(self) -> None:
        """
        Discovers all providers by scanning packages installed. The list of providers should be returned
        via the 'apache_airflow_provider' entrypoint as a dictionary conforming to the
        'airflow/provider_info.schema.json' schema. Note that the schema is different at runtime
        than provider.yaml.schema.json. The development version of provider schema is more strict and changes
        together with the code. The runtime version is more relaxed (allows for additional properties)
        and verifies only the subset of fields that are needed at runtime.
        """
        for entry_point, dist in entry_points_with_dist('apache_airflow_provider'):
            package_name = dist.metadata['name']
            if self._provider_dict.get(package_name) is not None:
                continue
            log.debug("Loading %s from package %s", entry_point, package_name)
            version = dist.version
            provider_info = entry_point.load()()
            self._provider_schema_validator.validate(provider_info)
            provider_info_package_name = provider_info['package-name']
            if package_name != provider_info_package_name:
                raise Exception(
                    f"The package '{package_name}' from setuptools and "
                    f"{provider_info_package_name} do not match. Please make sure they are aligned"
                )
            if package_name not in self._provider_dict:
                self._provider_dict[package_name] = ProviderInfo(version, provider_info)
            else:
                log.warning(
                    "The provider for package '%s' could not be registered from because providers for that "
                    "package name have already been registered",
                    package_name,
                )

    def _discover_all_airflow_builtin_providers_from_local_sources(self) -> None:
        """
        Finds all built-in airflow providers if airflow is run from the local sources.
        It finds `provider.yaml` files for all such providers and registers the providers using those.

        This 'provider.yaml' scanning takes precedence over scanning packages installed
        in case you have both sources and packages installed, the providers will be loaded from
        the "airflow" sources rather than from the packages.
        """
        try:
            import airflow.providers
        except ImportError:
            log.info("You have no providers installed.")
            return
        try:
            for path in airflow.providers.__path__:
                self._add_provider_info_from_local_source_files_on_path(path)
        except Exception as e:
            log.warning("Error when loading 'provider.yaml' files from airflow sources: %s", e)

    def _add_provider_info_from_local_source_files_on_path(self, path) -> None:
        """
        Finds all the provider.yaml files in the directory specified.

        :param path: path where to look for provider.yaml files
        """
        root_path = path
        for folder, subdirs, files in os.walk(path, topdown=True):
            for filename in fnmatch.filter(files, "provider.yaml"):
                package_name = "apache-airflow-providers" + folder[len(root_path) :].replace(os.sep, "-")
                self._add_provider_info_from_local_source_file(os.path.join(folder, filename), package_name)
                subdirs[:] = []

    def _add_provider_info_from_local_source_file(self, path, package_name) -> None:
        """
        Parses found provider.yaml file and adds found provider to the dictionary.

        :param path: full file path of the provider.yaml file
        :param package_name: name of the package
        """
        try:
            log.debug("Loading %s from %s", package_name, path)
            with open(path) as provider_yaml_file:
                provider_info = yaml.safe_load(provider_yaml_file)
            self._provider_schema_validator.validate(provider_info)

            version = provider_info['versions'][0]
            if package_name not in self._provider_dict:
                self._provider_dict[package_name] = ProviderInfo(version, provider_info)
            else:
                log.warning(
                    "The providers for package '%s' could not be registered because providers for that "
                    "package name have already been registered",
                    package_name,
                )
        except Exception as e:
            log.warning("Error when loading '%s': %s", path, e)

    def _discover_hooks(self) -> None:
        """Retrieves all connections defined in the providers"""
        for name, provider in self._provider_dict.items():
            provider_package = name
            hook_class_names = provider[1].get("hook-class-names")
            if hook_class_names:
                for hook_class_name in hook_class_names:
                    self._add_hook(hook_class_name, provider_package)

    @staticmethod
    def _get_attr(obj: Any, attr_name: str):
        """Retrieves attributes of an object, or warns if not found"""
        if not hasattr(obj, attr_name):
            log.warning("The '%s' is missing %s attribute and cannot be registered", obj, attr_name)
            return None
        return getattr(obj, attr_name)

    def _add_hook(self, hook_class_name: str, provider_package: str) -> None:
        """
        Adds hook class name to list of hooks

        :param hook_class_name: name of the Hook class
        :param provider_package: provider package adding the hook
        """
        if not _sanity_check(provider_package, hook_class_name):
            return
        if hook_class_name in self._hooks_dict:
            log.warning(
                "The hook_class '%s' has been already registered.",
                hook_class_name,
            )
            return
        try:
            module, class_name = hook_class_name.rsplit('.', maxsplit=1)
            hook_class = getattr(importlib.import_module(module), class_name)
            # Do not use attr here. We want to check only direct class fields not those
            # inherited from parent hook. This way we add form fields only once for the whole
            # hierarchy and we add it only from the parent hook that provides those!
            if 'get_connection_form_widgets' in hook_class.__dict__:
                widgets = hook_class.get_connection_form_widgets()
                if widgets:
                    for widget in widgets.values():
                        if widget.field_class not in ALLOWED_FIELD_CLASSES:
                            log.warning(
                                "The hook_class '%s' uses field of unsupported class '%s'. "
                                "Only '%s' field classes are supported",
                                hook_class_name,
                                widget.field_class,
                                ALLOWED_FIELD_CLASSES,
                            )
                            return
                    self._add_widgets(provider_package, hook_class, widgets)
            if 'get_ui_field_behaviour' in hook_class.__dict__:
                field_behaviours = hook_class.get_ui_field_behaviour()
                if field_behaviours:
                    self._add_customized_fields(provider_package, hook_class, field_behaviours)

        except ImportError as e:
            # When there is an ImportError we turn it into debug warnings as this is
            # an expected case when only some providers are installed
            log.debug(
                "Exception when importing '%s' from '%s' package: %s",
                hook_class_name,
                provider_package,
                e,
            )
            return
        except Exception as e:
            log.warning(
                "Exception when importing '%s' from '%s' package: %s",
                hook_class_name,
                provider_package,
                e,
            )
            return

        conn_type: str = self._get_attr(hook_class, 'conn_type')
        connection_id_attribute_name: str = self._get_attr(hook_class, 'conn_name_attr')
        hook_name: str = self._get_attr(hook_class, 'hook_name')

        if not conn_type or not connection_id_attribute_name or not hook_name:
            return

        self._hooks_dict[conn_type] = HookInfo(
            hook_class_name,
            connection_id_attribute_name,
            provider_package,
            hook_name,
        )

    def _add_widgets(self, package_name: str, hook_class: type, widgets: Dict[str, Field]):
        for field_name, field in widgets.items():
            if not field_name.startswith("extra__"):
                log.warning(
                    "The field %s from class %s does not start with 'extra__'. Ignoring it.",
                    field_name,
                    hook_class.__name__,
                )
                continue
            if field_name in self._connection_form_widgets:
                log.warning(
                    "The field %s from class %s has already been added by another provider. Ignoring it.",
                    field_name,
                    hook_class.__name__,
                )
                # In case of inherited hooks this might be happening several times
                continue
            self._connection_form_widgets[field_name] = ConnectionFormWidgetInfo(
                hook_class.__name__, package_name, field
            )

    def _add_customized_fields(self, package_name: str, hook_class: type, customized_fields: Dict):
        try:
            connection_type = getattr(hook_class, "conn_type")
            self._customized_form_fields_schema_validator.validate(customized_fields)
            if connection_type in self._field_behaviours:
                log.warning(
                    "The connection_type %s from package %s and class %s has already been added "
                    "by another provider. Ignoring it.",
                    connection_type,
                    package_name,
                    hook_class.__name__,
                )
                return
            self._field_behaviours[connection_type] = customized_fields
        except Exception as e:
            log.warning(
                "Error when loading customized fields from package '%s' hook class '%s': %s",
                package_name,
                hook_class.__name__,
                e,
            )

    def _discover_extra_links(self) -> None:
        """Retrieves all extra links defined in the providers"""
        for provider_package, (_, provider) in self._provider_dict.items():
            if provider.get("extra-links"):
                for extra_link_class_name in provider["extra-links"]:
                    if _sanity_check(provider_package, extra_link_class_name):
                        self._extra_link_class_name_set.add(extra_link_class_name)

    def _discover_logging(self) -> None:
        """Retrieves all logging defined in the providers"""
        for provider_package, (_, provider) in self._provider_dict.items():
            if provider.get("logging"):
                for logging_class_name in provider["logging"]:
                    if _sanity_check(provider_package, logging_class_name):
                        self._logging_class_name_set.add(logging_class_name)

    def _discover_secrets_backends(self) -> None:
        """Retrieves all secrets backends defined in the providers"""
        for provider_package, (_, provider) in self._provider_dict.items():
            if provider.get("secrets-backends"):
                for secrets_backends_class_name in provider["secrets-backends"]:
                    if _sanity_check(provider_package, secrets_backends_class_name):
                        self._secrets_backend_class_name_set.add(secrets_backends_class_name)

    def _discover_auth_backends(self) -> None:
        """Retrieves all API auth backends defined in the providers"""
        for provider_package, (_, provider) in self._provider_dict.items():
            if provider.get("auth-backends"):
                for auth_backend_module_name in provider["auth-backends"]:
                    if _sanity_check(provider_package, auth_backend_module_name + ".init_app"):
                        self._api_auth_backend_module_names.add(auth_backend_module_name)

    @property
    def providers(self) -> Dict[str, ProviderInfo]:
        """Returns information about available providers."""
        self.initialize_providers_list()
        return self._provider_dict

    @property
    def hooks(self) -> Dict[str, HookInfo]:
        """Returns dictionary of connection_type-to-hook mapping"""
        self.initialize_providers_hooks()
        return self._hooks_dict

    @property
    def extra_links_class_names(self) -> List[str]:
        """Returns set of extra link class names."""
        self.initialize_providers_extra_links()
        return sorted(self._extra_link_class_name_set)

    @property
    def connection_form_widgets(self) -> Dict[str, ConnectionFormWidgetInfo]:
        """Returns widgets for connection forms."""
        self.initialize_providers_hooks()
        return self._connection_form_widgets

    @property
    def field_behaviours(self) -> Dict[str, Dict]:
        """Returns dictionary with field behaviours for connection types."""
        self.initialize_providers_hooks()
        return self._field_behaviours

    @property
    def logging_class_names(self) -> List[str]:
        """Returns set of log task handlers class names."""
        self.initialize_providers_logging()
        return sorted(self._logging_class_name_set)

    @property
    def secrets_backend_class_names(self) -> List[str]:
        """Returns set of secret backend class names."""
        self.initialize_providers_secrets_backends()
        return sorted(self._secrets_backend_class_name_set)

    @property
    def auth_backend_module_names(self) -> List[str]:
        """Returns set of API auth backend class names."""
        self.initialize_providers_auth_backends()
        return sorted(self._api_auth_backend_module_names)
