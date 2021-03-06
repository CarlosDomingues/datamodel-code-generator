import re
from abc import ABC, abstractmethod
from collections import OrderedDict, defaultdict
from functools import lru_cache
from itertools import groupby
from pathlib import Path
from typing import (
    Any,
    Callable,
    DefaultDict,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
)

from pydantic import BaseModel

from datamodel_code_generator.format import CodeFormatter

from ..format import PythonVersion
from ..imports import IMPORT_ANNOTATIONS, Import, Imports
from ..model import pydantic as pydantic_model
from ..model.base import ALL_MODEL, DataModel, DataModelFieldBase
from ..model.enum import Enum
from ..reference import ModelResolver
from ..types import DataType, DataTypeManager
from . import LiteralType

_UNDER_SCORE_1 = re.compile(r'(.)([A-Z][a-z]+)')
_UNDER_SCORE_2 = re.compile('([a-z0-9])([A-Z])')


@lru_cache()
def camel_to_snake(string: str) -> str:
    subbed = _UNDER_SCORE_1.sub(r'\1_\2', string)
    return _UNDER_SCORE_2.sub(r'\1_\2', subbed).lower()


def to_hashable(item: Any) -> Any:
    if isinstance(item, list):
        return tuple(to_hashable(i) for i in item)
    elif isinstance(item, dict):
        return tuple(sorted((k, to_hashable(v),) for k, v in item.items()))
    elif isinstance(item, set):
        return frozenset(to_hashable(i) for i in item)
    elif isinstance(item, BaseModel):
        return to_hashable(item.dict())
    return item


def snakify_field(field: DataModelFieldBase) -> None:
    if not field.name:
        return
    original_name = field.name
    field.name = camel_to_snake(original_name)
    if field.name != original_name:
        field.alias = original_name


def set_strip_default_none(field: DataModelFieldBase) -> None:
    field.strip_default_none = True


def dump_templates(templates: List[DataModel]) -> str:
    return '\n\n\n'.join(str(m) for m in templates)


ReferenceMapSet = Dict[str, Set[str]]
SortedDataModels = Dict[str, DataModel]

MAX_RECURSION_COUNT: int = 100


def sort_data_models(
    unsorted_data_models: List[DataModel],
    sorted_data_models: Optional[SortedDataModels] = None,
    require_update_action_models: Optional[List[str]] = None,
    recursion_count: int = MAX_RECURSION_COUNT,
) -> Tuple[List[DataModel], SortedDataModels, List[str]]:
    if sorted_data_models is None:
        sorted_data_models = OrderedDict()
    if require_update_action_models is None:
        require_update_action_models = []

    unresolved_references: List[DataModel] = []
    for model in unsorted_data_models:
        if not model.reference_classes:
            sorted_data_models[model.name] = model
        elif (
            model.name in model.reference_classes and len(model.reference_classes) == 1
        ):  # only self-referencing
            sorted_data_models[model.name] = model
            require_update_action_models.append(model.name)
        elif (
            not model.reference_classes - {model.name} - set(sorted_data_models)
        ):  # reference classes have been resolved
            sorted_data_models[model.name] = model
            if model.name in model.reference_classes:
                require_update_action_models.append(model.name)
        else:
            unresolved_references.append(model)
    if unresolved_references:
        if recursion_count:
            try:
                return sort_data_models(
                    unresolved_references,
                    sorted_data_models,
                    require_update_action_models,
                    recursion_count - 1,
                )
            except RecursionError:
                pass

        # sort on base_class dependency
        while True:
            ordered_models: List[Tuple[int, DataModel]] = []
            unresolved_reference_model_names = [m.name for m in unresolved_references]
            for model in unresolved_references:
                indexes = [
                    unresolved_reference_model_names.index(b)
                    for b in model.base_classes
                    if b in unresolved_reference_model_names
                ]
                if indexes:
                    ordered_models.append((min(indexes), model,))
                else:
                    ordered_models.append((-1, model,))
            sorted_unresolved_models = [
                m[1] for m in sorted(ordered_models, key=lambda m: m[0])
            ]
            if sorted_unresolved_models == unresolved_references:
                break
            unresolved_references = sorted_unresolved_models

        # circular reference
        unsorted_data_model_names = set(unresolved_reference_model_names)
        for model in unresolved_references:
            unresolved_model = (
                model.reference_classes - {model.name} - set(sorted_data_models)
            )
            if not unresolved_model:
                sorted_data_models[model.name] = model
                continue
            if not unresolved_model - unsorted_data_model_names:
                sorted_data_models[model.name] = model
                require_update_action_models.append(model.name)
                continue
            # unresolved
            unresolved_classes = ', '.join(
                f"[class: {item.name} references: {item.reference_classes}]"
                for item in unresolved_references
            )
            raise Exception(f'A Parser can not resolve classes: {unresolved_classes}.')
    return unresolved_references, sorted_data_models, require_update_action_models


def relative(current_module: str, reference: str) -> Tuple[str, str]:
    """Find relative module path."""

    current_module_path = current_module.split('.') if current_module else []
    *reference_path, name = reference.split('.')

    if current_module_path == reference_path:
        return '', ''

    i = 0
    for x, y in zip(current_module_path, reference_path):
        if x != y:
            break
        i += 1

    left = '.' * (len(current_module_path) - i)
    right = '.'.join(reference_path[i:])

    if not left:
        left = '.'
    if not right:
        right = name
    elif '.' in right:
        extra, right = right.rsplit('.', 1)
        left += extra

    return left, right


class Result(BaseModel):
    body: str
    source: Optional[Path]


class Source(BaseModel):
    path: Path
    text: str

    @classmethod
    def from_path(cls, path: Path, base_path: Path, encoding: str) -> 'Source':
        return cls(
            path=path.relative_to(base_path), text=path.read_text(encoding=encoding),
        )


class Parser(ABC):
    def __init__(
        self,
        source: Union[str, Path, List[Path]],
        *,
        data_model_type: Type[DataModel] = pydantic_model.BaseModel,
        data_model_root_type: Type[DataModel] = pydantic_model.CustomRootType,
        data_type_manager_type: Type[DataTypeManager] = pydantic_model.DataTypeManager,
        data_model_field_type: Type[DataModelFieldBase] = pydantic_model.DataModelField,
        base_class: Optional[str] = None,
        custom_template_dir: Optional[Path] = None,
        extra_template_data: Optional[DefaultDict[str, Dict[str, Any]]] = None,
        target_python_version: PythonVersion = PythonVersion.PY_37,
        dump_resolve_reference_action: Optional[Callable[[List[str]], str]] = None,
        validation: bool = False,
        field_constraints: bool = False,
        snake_case_field: bool = False,
        strip_default_none: bool = False,
        aliases: Optional[Mapping[str, str]] = None,
        allow_population_by_field_name: bool = False,
        apply_default_values_for_required_fields: bool = False,
        force_optional_for_required_fields: bool = False,
        class_name: Optional[str] = None,
        use_standard_collections: bool = False,
        base_path: Optional[Path] = None,
        use_schema_description: bool = False,
        reuse_model: bool = False,
        encoding: str = 'utf-8',
        enum_field_as_literal: Optional[LiteralType] = None,
    ):
        self.data_type_manager: DataTypeManager = data_type_manager_type(
            target_python_version, use_standard_collections
        )
        self.data_model_type: Type[DataModel] = data_model_type
        self.data_model_root_type: Type[DataModel] = data_model_root_type
        self.data_model_field_type: Type[DataModelFieldBase] = data_model_field_type
        self.imports: Imports = Imports()
        self.base_class: Optional[str] = base_class
        self.target_python_version: PythonVersion = target_python_version
        self.results: List[DataModel] = []
        self.dump_resolve_reference_action: Optional[
            Callable[[List[str]], str]
        ] = dump_resolve_reference_action
        self.validation: bool = validation
        self.field_constraints: bool = field_constraints
        self.snake_case_field: bool = snake_case_field
        self.strip_default_none: bool = strip_default_none
        self.apply_default_values_for_required_fields: bool = apply_default_values_for_required_fields
        self.force_optional_for_required_fields: bool = force_optional_for_required_fields
        self.use_schema_description: bool = use_schema_description
        self.reuse_model: bool = reuse_model
        self.encoding: str = encoding
        self.enum_field_as_literal: Optional[LiteralType] = enum_field_as_literal

        self.current_source_path: Optional[Path] = None

        if base_path:
            self.base_path = base_path
        elif isinstance(source, Path):
            self.base_path = (
                source.absolute() if source.is_dir() else source.absolute().parent
            )
        else:
            self.base_path = Path.cwd()

        self.source: Union[str, Path, List[Path]] = source
        self.custom_template_dir = custom_template_dir
        self.extra_template_data: DefaultDict[
            str, Any
        ] = extra_template_data or defaultdict(dict)

        if allow_population_by_field_name:
            self.extra_template_data[ALL_MODEL]['allow_population_by_field_name'] = True

        self.model_resolver = ModelResolver(aliases=aliases)
        self.field_preprocessors: List[Callable[[DataModelFieldBase], None]] = []
        if self.snake_case_field:
            self.field_preprocessors.append(snakify_field)
        if self.strip_default_none:
            self.field_preprocessors.append(set_strip_default_none)
        self.class_name: Optional[str] = class_name

    @property
    def iter_source(self) -> Iterator[Source]:
        if isinstance(self.source, str):
            yield Source(path=Path(), text=self.source)
        elif isinstance(self.source, Path):  # pragma: no cover
            if self.source.is_dir():
                for path in self.source.rglob('*'):
                    if path.is_file():
                        yield Source.from_path(path, self.base_path, self.encoding)
            else:
                yield Source.from_path(self.source, self.base_path, self.encoding)
        elif isinstance(self.source, list):  # pragma: no cover
            for path in self.source:
                yield Source.from_path(path, self.base_path, self.encoding)

    def append_result(self, data_model: DataModel) -> None:
        for field_preprocessor in self.field_preprocessors:
            for field in data_model.fields:
                field_preprocessor(field)
        self.results.append(data_model)

    @property
    def data_type(self) -> Type[DataType]:
        return self.data_type_manager.data_type

    @abstractmethod
    def parse_raw(self) -> None:
        raise NotImplementedError

    def parse(
        self,
        with_import: Optional[bool] = True,
        format_: Optional[bool] = True,
        settings_path: Optional[Path] = None,
    ) -> Union[str, Dict[Tuple[str, ...], Result]]:

        self.parse_raw()

        if with_import:
            if self.target_python_version != PythonVersion.PY_36:
                self.imports.append(IMPORT_ANNOTATIONS)

        if format_:
            code_formatter: Optional[CodeFormatter] = CodeFormatter(
                self.target_python_version, settings_path
            )
        else:
            code_formatter = None

        _, sorted_data_models, require_update_action_models = sort_data_models(
            self.results
        )

        results: Dict[Tuple[str, ...], Result] = {}

        module_key = lambda x: x.module_path

        # process in reverse order to correctly establish module levels
        grouped_models = groupby(
            sorted(sorted_data_models.values(), key=module_key, reverse=True),
            key=module_key,
        )
        for module, models in (
            (k, [*v]) for k, v in grouped_models
        ):  # type: Tuple[str, ...], List[DataModel]
            module_path = '.'.join(module)

            init = False
            if module:
                parent = (*module[:-1], '__init__.py')
                if parent not in results:
                    results[parent] = Result(body='')
                if (*module, '__init__.py') in results:
                    module = (*module, '__init__.py')
                    init = True
                else:
                    module = (*module[:-1], f'{module[-1]}.py')
            else:
                module = ('__init__.py',)

            result: List[str] = []
            imports = Imports()
            models_to_update: List[str] = []
            scoped_model_resolver = ModelResolver()
            import_map: Dict[str, Tuple[str, str]] = {}
            model_names: Set[str] = {m.name for m in models}
            processed_models: Set[str] = set()
            model_cache: Dict[Tuple[str, ...], str] = {}
            duplicated_model_names: Dict[str, str] = {}
            for model in models:
                alias_map: Dict[str, Optional[str]] = {}
                if model.name in require_update_action_models:
                    ref_names = {
                        d.reference.name for d in model.all_data_types if d.reference
                    }
                    if model.name in ref_names or (
                        not ref_names - model_names and ref_names - processed_models
                    ):
                        models_to_update += [model.name]
                        processed_models.add(model.name)
                imports.append(model.imports)
                for data_type in model.all_data_types:
                    if not data_type.is_modular:
                        continue
                    full_name = data_type.full_name
                    from_, import_ = relative(module_path, full_name)
                    name = data_type.name
                    if data_type.reference:
                        reference = self.model_resolver.get(data_type.reference.path)
                        if reference and (
                            (
                                isinstance(self.source, Path)
                                and self.source.is_file()
                                and self.source.name == reference.path.split('#/')[0]
                            )
                            or reference.actual_module_name == module_path
                        ):
                            if name in model.reference_classes:  # pragma: no cover
                                model.reference_classes.remove(name)
                            continue
                    full_path = f'{from_}/{import_}'
                    if full_path in alias_map:
                        alias = alias_map[full_path] or import_
                    else:
                        alias = scoped_model_resolver.add(
                            full_path.split('/'), import_, unique=True
                        ).name
                        alias_map[full_path] = None if alias == import_ else alias
                    new_name = f'{alias}.{name}' if from_ and import_ else name
                    if data_type.module_name and not full_name.startswith(from_):
                        import_map[new_name] = (
                            f'.{full_name[:len(new_name) * - 1 - 1]}',
                            new_name.split('.')[0],
                        )
                    if name in model.reference_classes:
                        model.reference_classes.remove(name)
                        model.reference_classes.add(new_name)
                    data_type.type = new_name

                for ref_name in model.reference_classes:
                    if ref_name in model_names:
                        continue
                    elif ref_name in import_map:
                        from_, import_ = import_map[ref_name]
                    else:
                        from_, import_ = relative(module_path, ref_name)
                    if init:
                        from_ += "."
                    imports.append(
                        Import(
                            from_=from_,
                            import_=import_,
                            alias=alias_map.get(f'{from_}/{import_}'),
                        )
                    )

                if self.reuse_model:
                    model_key = tuple(
                        to_hashable(v)
                        for v in (
                            model.base_classes,
                            model.extra_template_data,
                            model.fields,
                        )
                    )
                    cached_model_name = model_cache.get(model_key)
                    if cached_model_name:
                        if isinstance(model, Enum):
                            duplicated_model_names[model.name] = cached_model_name
                        else:
                            index = models.index(model)
                            inherited_model = model.__class__(
                                name=model.name,
                                fields=[],
                                base_classes=[cached_model_name],
                                description=model.description,
                            )
                            models.insert(index, inherited_model)
                            models.remove(model)

                    else:
                        model_cache[model_key] = model.name

            if self.reuse_model:
                for model in models:
                    for data_type in model.all_data_types:  # pragma: no cover
                        if data_type.type:
                            duplicated_model_name = duplicated_model_names.get(
                                data_type.type
                            )
                            if duplicated_model_name:
                                data_type.type = duplicated_model_name

            if with_import:
                result += [str(imports), str(self.imports), '\n']

            code = dump_templates(models)
            result += [code]

            if self.dump_resolve_reference_action is not None:
                result += ['\n', self.dump_resolve_reference_action(models_to_update)]

            body = '\n'.join(result)
            if code_formatter:
                body = code_formatter.format_code(body)

            results[module] = Result(body=body, source=models[0].path)

        # retain existing behaviour
        if [*results] == [('__init__.py',)]:
            return results[('__init__.py',)].body

        return results
