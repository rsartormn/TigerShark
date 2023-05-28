"""
Base definitions for X12 message constructs:

- Element -- the atomic base value -- A python object -- str, int, float, Decimal

- Composite -- a sequence of Elements

- Segment -- a sequence of [Composite | Element] with a prefix code (e.g., "ISA", "IEA")

- Loop -- a sequence of [Loop | Segment]

- Message -- a sequence of Loop

Elements have an element separator (often "|")

Composites seem to have a component separator (varies widely, but ":" is common)

Segments have a segment separator (often "~")

The various X12 details are implemented as ``Annotated[...]``
type hints.

Design
======

The top-level (Message, Loop, Segment) structures all have a :meth:`parse` method
to parse a :meth:`Source` instance. This consumes whole segments.

The bottom-level (Composite) structure has a :meth:`build` method that
consumes oen or more field values from a parsed segment.
"""
from collections import defaultdict
from collections.abc import Iterator, Callable
import datetime
from decimal import Decimal
import fnmatch
from functools import wraps, cache
import json
import logging
import sys
from textwrap import dedent
from types import GenericAlias, UnionType, NoneType, FunctionType
from typing import (
    Any, cast,
    get_type_hints, get_origin, get_args, assert_type,
    Union, TypeAlias, Annotated, Optional, DefaultDict,
    _SpecialForm, _UnionGenericAlias,
)
from typing_extensions import _AnnotatedAlias
import warnings

from .annotations import *

logger = logging.getLogger("x12.base")


class Source:
    """
    The source text used to parse a message in "Exchange format."

    High-level client's :meth:`parse` method will consume an entire segment, up to the segment separator.
    This applies to :class:`Method`, :class:`Loop`, and :class:`Segment`.
    The :meth:`next_segment` method lets a client peek at the prefix to the next segment.

    Low-level client's :meth:`build` method will consume element values from a segment.
    The :meth:`elements` method decomposes the current segment, and advances to the next segment.

    State invariant: ``self.text[self.pos]`` is the start of a segment.

    When parsing the initial ISA segment, there's no defined ``element_sep``.
    During parsing the ISA, the ``element_sep`` and ``segment_sep`` can be located,
    and used for all following segments. The ``array_sep`` (or component separator) is explicitly
    provided in the ISA segment; this is used to decompose composites.

    In some rare cases, the ISA is already compressed. The element and segment
    separation characters must be provided separately.

    ..  todo:: Consider str | TextIO | Path as ``text`` parameter types.
    """
    logger = logging.getLogger("x12.base.Source")

    def __init__(self, text: str, element_sep: str = "", segment_sep: str = "", array_sep: str = "") -> None:
        self.text = text
        self.element_sep = element_sep  # Often "|", might be "*".
        self.segment_sep = segment_sep  # Often "~".
        # The Component_Separator is the value of ISA16. It is not, generally, set in advance.
        self.array_sep = array_sep  # Often ":". Sometimes "^".
        self.pos: int = 0

    def consume_whitespace(self) -> None:
        """
        Moves ``self.pos`` past any leading whitespace.
        """
        while self.pos != len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def elements(self) -> list[str | list[str]]:
        """
        Unpack a Segment into a list of potential Element values.
        This consumes whitespace.
        It consumes the segment up to (and including) the segment_sep character.

        Note the initial ISA segment is fixed length, not compressed. Separators aren't defined yet.
        The initial ISA segment's text is returned as a single string for parsing by the segment.

        Most elements are atomic with two notable exceptions:

        -   There array array-like elements that leverage
            the ISA16 Component Separator character, called the
            ``array_sep`` here.

        -   Composites are punctuated by the ISA16 Component Separator character.

        We choose to parse these non-atomic elements here by looking at the values
        and splitting any that have the separator character.

        Conceptually:

            >>> source = "field|comp:osite|field~"
            >>> segments = source.split("~")
            >>> fields = segments[0].split("|")
            >>> fields = [v.split(":") if ":" in v else v for v in fields]
            >>> fields
            ['field', ['comp', 'osite'], 'field']

        ..  todo:: Optimize the field and composite splitting to avoid creating multiple lists.
        """
        if not self.segment_sep:
            raise RuntimeError("no element or segment separators set")  # pragma: no cover
        self.consume_whitespace()
        # TODO: re.search() might be faster at finding the next segment separator
        start = self.pos
        while self.pos != len(self.text) and self.text[self.pos] != self.segment_sep:
            self.pos += 1
        # assert self.text[self.pos] == self.segment_sep
        if self.pos != len(self.text):
            self.pos += 1
        elements = self.text[start:self.pos-1].rstrip().split(self.element_sep)
        # ISA segment? Do NOT subdivide ISA16 field's value -- it would vanish into ``["", ""]``.
        # TODO: Segment ISA parser can replace ISA16 ``["", ""]`` value with the parsed array_sep value.
        if elements[0].upper() == "ISA":
            return cast(list[str | list[str]], elements)
        return [
            val.split(self.array_sep) if self.array_sep in val else val
            for val in elements
        ]

    def peek(self, size: int = 106) -> str:
        """
        Peek ahead a fixed number of characters. This also consumes leading whitespace.
        The default peek is 106 to read a non-compressed ISA segment.
        """
        self.consume_whitespace()
        # Handle the weird case of segment separator **preceeded** by whitespace.
        end = self.pos + size
        segment = self.text[self.pos: end].rstrip()
        while (
                len(segment) != size and
                end != len(self.text)
        ):
            self.logger.debug("Whitespace before segment separator")
            if not self.text[end].isspace():
                segment += self.text[end]
            end += 1
        return segment

    def next_segment(self) -> str:
        """Peek at upcoming segment's header. This consumes leading whitespace."""
        self.consume_whitespace()
        if not self.element_sep:
            # Assume a 3-char prefix until we have a separator
            return self.text[self.pos: self.pos+3]
        # TODO: re.search() might be faster at finding the next element separator
        start = end = self.pos
        while end != len(self.text) and self.text[end] != self.element_sep:
            end += 1
        return self.text[start: end]

    def __repr__(self) -> str:
        return f"{self.element_sep=} {self.segment_sep=} text={self.text[:self.pos]!r} && {self.text[self.pos:]!r}"


Conversion: TypeAlias = Callable[[str], Any] | Callable[[str, str], Any] | type[Any]
Formatter: TypeAlias = Callable[[Any], str]


class X12TypeHelper:
    """
    Helper to convert element values from source text
    to Python objects, and Python back to source text.

    Don't create these directly. Use the ``annotated`` method.

    ::

        hints = get_type_hints(SomeSegment, include_extras=True)
        some_helper = base.X12TypeHelper.annotated(hints['some_element'])


    The :py:meth:`validate` method raises a :exc:`ValueError`
    exception  with two parameters:

    -   The usual message.

    -   The Annotation class name that failed validation.

    ..  TODO: Should be a  hierarchy of classes.

        The "format" feature should be subclasses not method plug-ins.
    """

    @classmethod
    def annotated(cls, type_hint: type[Any]) -> "X12TypeHelper":
        """
        Build an X12TypeHelper to convert text to Python objects,
        and python objects back to text.
        """
        try:
            base, *aspects = get_args(type_hint)
        except ValueError:
            base = type_hint
            aspects = []
        # print(f"annotated({type_hint}) {base=} {aspects=}")
        match base:
            case type() if base == str:
                return X12TypeHelper(str, *aspects, formatter=cls.str_fmt)
            case type() if base == int:
                return X12TypeHelper(int, *aspects, formatter=cls.int_fmt)
            case type() if base == float:
                return X12TypeHelper(float, *aspects, formatter=cls.float_fmt)
            case type() if base == Decimal:
                scale_aspect = [a for a in aspects if isinstance(a, Scale)]
                if scale_aspect:
                    scale = int(scale_aspect[0].params[0])
                else:  # pragma: no cover
                    scale = 0
                decimal_conversion: Conversion = cast(
                        Conversion,
                        lambda to_decimal=None, scale=scale: (Decimal(to_decimal) if to_decimal else Decimal()).scaleb(-scale)
                )
                return X12TypeHelper(
                    decimal_conversion,
                    *aspects,
                    formatter=cls.dec_fmt
                )
            case type() if base == datetime.date:
                # TODO: Make this sensisitve to MinLen() and MaxLen()
                return X12TypeHelper(
                    lambda x, f: datetime.datetime.strptime(x, f).date(),
                    *aspects,
                    parse_formats=["%y%m%d", "%Y%m%d",],
                    formatter=cls.dttm_fmt
                )
            case type() if base == datetime.time:
                # TODO: Make this sensisitve to MinLen() and MaxLen()
                return X12TypeHelper(
                    lambda x, f: datetime.datetime.strptime(x, f).time(),
                    *aspects,
                    parse_formats=["%H%M", "%H%M%S",],
                    formatter=cls.dttm_fmt
                )
            case type() if issubclass(base, Composite):
                return X12TypeHelper(cast(Conversion, lambda x: x), *aspects)
            # Now it gets ugly: GenericAlias/UnionType/_AnnotatedAlias
            case GenericAlias() if get_origin(base) is list:
                repeating, *aspects = get_args(base)
                item_helper = cls.annotated(repeating)
                # A "list[X]" to_py that checks MaxItems (and MinItems)
                list_conversion: Conversion = cast(
                    Conversion,
                    lambda some_list: [item_helper.to_py(v) for v in some_list]
                )
                return X12TypeHelper(list_conversion, *aspects)
            case UnionType():
                alternatives = get_args(base)
                # TODO: Need a X | Y | None to_py construct.
                # If a validate() works, that's the result, None.
                # If none of the validate()'s work, and there's a None, that's it.
                # Otherwise, a ValueError exception from a failing validate.
                return cls.annotated(alternatives[0])
            case _AnnotatedAlias():
                # An "Annotated[X]" to_py
                base, *details = get_args(base)
                base_helper = cls.annotated(base)
                return X12TypeHelper(lambda x: base_helper.to_py(x), *details)
        raise TypeError(f"invalid {type_hint}, base class {base} unknown")

    def __init__(self, conversion: Conversion, *aspects: X12Annotation, parse_formats: list[str] | None = None, formatter: Formatter | None = None) -> None:
        self.conversion = conversion  # the to-python conversion function
        self.formatter = formatter or str  # the to-text formatter function, a default `str()` is used if this is omitted.
        self.aspects = aspects  # The annotations that will be checked.
        # TODO: Seems to be a silly optimization.
        self.validation = [
            a
            for a in self.aspects
            if a.validation
        ]  # Subset of annotations that can be validated.
        self.optional = (
            any(
                a.params[0] in {"N", "S"} for a in self.aspects
                if isinstance(a, (Usage,))
            ) or any(
                not a.params[0] for a in self.aspects
                if isinstance(a, (Required,))
            )
        )  # Interpret X12 rules to see if this is optional
        self.parse_formats: list[str] = parse_formats or []  # Alternative date/time formats.
        self.dttm_format: str | None = None  # Set to the preferred cormat for dates or times.
        self.max_len = self._value_for(MaxLen) or 99
        self.min_len = self._value_for(MinLen) or 0
        self.scale = self._value_for(Scale)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.conversion}, *{self.aspects}, parse_formats={self.parse_formats or None}, formatter={self.formatter})"

    def _value_for(self, annotation_class: type[X12Annotation]) -> Any:
        """Find the last value set for a specific annotation class."""
        instances = [a for a in self.aspects if isinstance(a, annotation_class)]
        if instances:
            return instances[-1].params[0]
        else:
            return None

    def validate(self, source: str) -> None:
        if self.optional and (source == '' or source is None):
            return
        for a in self.validation:
            if msg := a.invalid(source):
                raise ValueError(msg, a.__class__.__name__)

    def to_py(self, source: str) -> Any:
        if source is None:
            # TODO: Check ``Usage`` and ``Required`` annotations
            return None
        conv_func = self.conversion
        if self.parse_formats:
            # Date/Time with multiple formats.
            for fmt in self.parse_formats:
                try:
                    v = conv_func(source, fmt)
                    self.dttm_format = fmt
                    return v
                except ValueError:
                    pass
            raise ValueError(f"invalid {source!r} formats {self.parse_formats}")  # pragma: no cover
        else:
            # Other types without format alternatives.
            if source == "":
                # Assume the conversion is a type, str, int, float, Decimal
                try:
                    return cast(type[Any], conv_func)()
                except TypeError as ex:  # pragma: no cover
                    # Rare issue with conversion function being used improperly.
                    raise TypeError(f"{self.aspects} {ex}")
            else:
                return conv_func(source)

    def to_str(self, value: Any) -> str:
        # print(f"in {self!r}, to_str({value=}) with {self.formatter=}")
        return self.formatter(self, value)

    def str_fmt(self, value: Any) -> str:
        return f"{value:{self.min_len}s}"

    def int_fmt(self, value: Any) -> str:
        return f"{value:0>{self.min_len}d}"

    def float_fmt(self, value: Any) -> str:
        return f"{value:0>{self.min_len}.0f}"

    def dec_fmt(self, value: Any) -> str:
        base_text = f"{value:.{self.scale}f}".replace(".", "")
        return f"{base_text:0>{self.min_len}s}"

    def dttm_fmt(self, value: Any) -> str:
        if not self.dttm_format:
            self.dttm_format = self.parse_formats[0]
        return f"{value:{self.dttm_format}}"


SegmentText: TypeAlias = list[str | None | list[str | None]]


def schema(some_type: type[Any]) -> dict[str, Any]:
    """
    Expands an annotated type or any of the X12 structures into a JSON Schema.
    """

    def x12_class_base_schema(some_type: type[Any]) -> dict[str, Any]:
        properties = {
            name: schema(field_type)
            for name, field_type in class_fields(some_type)
        }
        json_schema = {
            "description": dedent(some_type.__doc__.strip()) if some_type.__doc__ else None,
            "class_name": some_type.__name__ if isinstance(some_type, type) else some_type.__class__.__name__,
            "type": "object",
            "properties": properties,
        }
        return json_schema

    annotations: list[X12Annotation] = []
    match some_type:
        case GenericAlias() if get_origin(some_type) is list:
            repeating_type, *annotations = get_args(some_type)
            json_schema = {
                "type": "array",
                "items": schema(repeating_type),
            }
        case _AnnotatedAlias():  # type: ignore [misc]
            base_type, *annotations = get_args(some_type)
            json_schema = schema(base_type)
        case UnionType() | _UnionGenericAlias():
            alternatives = get_args(some_type)
            json_schema = {
                "any": [schema(alt) for alt in alternatives]
            }
        case type() if issubclass(some_type, (Composite, Segment, Loop, Message)):
            json_schema = x12_class_base_schema(some_type)
        # Classes with built-in primitive schema.
        case type() if some_type is str:
            json_schema = {"type": "string"}
        case type() if some_type == int:
            json_schema = {"type": "integer"}
        case type() if some_type == float:
            json_schema = {"type": "number"}
        case type() if some_type == datetime.date:
            # TODO: X12TypeHelper has formats.
            json_schema = {"type": "string", "convert": "datetime.date", "pyformat": "%Y%m%d"}
        case type() if some_type == datetime.time:
            json_schema = {"type": "string", "convert": "datetime.time", "pyformat": "%h%m"}
        case type() if some_type == Decimal:
            json_schema = {"type": "string", "convert": "Decimal", "scale": "?"}
        case type() if some_type is NoneType:
            json_schema = {"type": "null"}
        case _:  # pragma: no cover
            raise ValueError(f"can't make schema for {some_type} ({type(some_type)})")
    # Effectively a reduce(set.union, (ann.JSONSchema for ann in annotations if hasattr(ann, "JSONSchema")))
    for ann in annotations:
        if hasattr(ann, "JSONSchema"):
            json_schema |= ann.JSONSchema
    return json_schema

X12Structure: TypeAlias = Union[
    "Composite", "Segment", "Loop", "Message",
    str, int, float, Decimal, datetime.date, datetime.time,
    None
]

def asdict(x12_obj: X12Structure) -> dict[str, Any] | Any:
    match x12_obj:
        case Composite() | Segment() | Loop() | Message():
            fields = {
                name: getattr(x12_obj, name, None)
                for name, hint in class_fields(x12_obj.__class__)
            }
            non_null_fields = {
                name: asdict(value)
                for name, value in fields.items()
                if value is not None
            }
            parent = x12_obj.__class__
            return {
                '_kind': x12_obj.__class__.__mro__[1].__name__,
                '_name': x12_obj.__class__.__name__,
            } | non_null_fields
        case str() | int() | float():
            return x12_obj
        case list(some_items):
            return [
                asdict(item) for item in some_items
            ]
        case _:  # pragma: no cover
            raise ValueError(f"can't create value for {x12_obj} ({type(x12_obj)})")


@cache
def class_fields(cls: type[Any]) -> list[str, type[Any]]:
    """
    Filtered subset of type hints for a class (Message, Loop, Segment, Composite).

    Excludes "Schema" as a special attribute name.

    Excludes all names starting with "_".

    Excludes any TypeAlias lines in the class.
    """
    fields = get_type_hints(cls, include_extras=True)
    # Exclude "Schema" and names starting with _
    x_schema_hidden = (
        (name, field_type)
        for name, field_type in fields.items()
        if not (name in {"Schema"} or name.startswith("_"))
    )
    # Exclude the TypeAlias special form.
    x_special = (
        (name, field_type)
        for name, field_type in x_schema_hidden
        if not (isinstance(field_type, _SpecialForm) and field_type._name == "TypeAlias")
    )
    return list(x_special)


class Composite:
    """
    A composite of values separated the "array_sep".

    Souce may have ``value|comp:osite|value``
    where the composite will be parsed into ``["comp", "osite"]``
    """
    _composite_name: str
    _helpers: dict[str, X12TypeHelper]
    _skip_validation: DefaultDict[str, list[str]]
    logger = logging.getLogger("x12.base.Composite")

    def __init__(self, **arg_dict) -> None:
        fields = get_type_hints(self.__class__, include_extras=True)
        unknowns = arg_dict.keys() - fields.keys()
        if unknowns:
            raise TypeError(f"unknown fields: {unknowns}")  # pragma: no cover
        self.__class__.make_helpers(fields)

        # Ideally, all missing names are also not required.
        for name, hint in class_fields(self.__class__):
            # Validate (possibly raising ValueError)
            try:
                self._helpers[name].validate(arg_dict.get(name))
            except ValueError as ex:
                if not (name in self._skip_validation and len(ex.args) == 2):
                    raise ValueError(f"invalid {self.__class__.__name__}.{name}: {hint} {ex}") from ex
                msg, annotation = ex.args
                if annotation not in self._skip_validation[name]:
                    raise ValueError(f"invalid {self.__class__.__name__}.{name}: {hint} {ex}") from ex
            # Build the attribute value
            value = self._helpers[name].to_py(arg_dict.get(name))
            setattr(self, name, value)

    @classmethod
    def make_helpers(cls, fields: dict[str, type(Any)], instance: Union["Composite", None] = None) -> None:
        """
        Build the element ``_helpers`` for this Composite.

        Used like this ``Composite.make_helpers(fields)``

        ..  todo:: Remove ``instance`` parameter
        """
        if hasattr(cls, '_helpers'):
            return

        cls._helpers = {
            name: X12TypeHelper.annotated(hint)
            for name, hint in class_fields(cls)
        }

    @classmethod
    def composite_configure(cls, skip_validation : DefaultDict[str, list[str]], segment_rules: list[tuple[str, str]], composite_name: str) -> None:
        cls._skip_validation = skip_validation
        fields = get_type_hints(cls, include_extras=True)
        cls.make_helpers(fields)
        for name, helper in cls._helpers.items():
            for field_match, annotation_name in segment_rules:
                if fnmatch.fnmatch(name, field_match):
                    cls._skip_validation[name].append(annotation_name)

    @classmethod
    def attr_build(cls, field_type: type, source_value: str | None) -> "Element":
        """Populate Element or Element | None field value"""
        match field_type:
            case UnionType():
                alternatives = list(filter(lambda t: t is not NoneType, get_args(field_type)))
                if len(alternatives) != 1:
                    raise NotImplementedError(f"not supported {field_type}; too many {alternatives}")  # pragma: no cover
                return alternatives[0].build(source_value)
            case _AnnotatedAlias():
                return source_value
            case type():
                return source_value
        raise TypeError(f"unexpected type {field_type}")  # pragma: no cover

    @classmethod
    def build(cls: type["Composite"], source_value: str | None | list[str | None]) -> "Composite":
        fields = get_type_hints(cls, include_extras=True)
        cls.logger.debug("build %s with %s", cls.__name__, fields)
        # Expand single value to a list
        if not isinstance(source_value, list):
            source_value = [source_value]

        # Pad with None instances?
        num_fields = sum(1 for name in class_fields(cls))
        if num_fields < len(source_value):
            # Too many values for the fields of this composite.
            raise ValueError(
                f"wrong number of values {cls.__name__} needs {num_fields} provided {len(source_value)}"
            )  # pragma: no cover
        elif num_fields > len(source_value):
            source_value.extend([None] * (num_fields - len(source_value)))
        else:
            # assert num_fields == len(source_value)
            pass

        # Build the object
        arg_dict = {
            name: cls.attr_build(field_type, source_value.pop(0))
            for name, field_type in class_fields(cls)
        }
        return cls(**arg_dict)

    def elements(self) -> list[str | None | list[str | None]]:
        fields = get_type_hints(self)
        return [getattr(self, name) for name, type_hint in class_fields(self.__class__)]

    def __repr__(self) -> str:
        good_names = (name for name, field_type in class_fields(self.__class__))
        text = ", ".join(f"{name}={getattr(self, name)!r}" for name in good_names)
        return f"{self.__class__.__name__}({text})"


class Segment:
    """
    A Segment is a collection of ``Elements | Composite``.

    Composites have array_sep separators.
    The lexical scanner turns these into sub-lists of the element value list.
    So ``element1|comp:osite|element3`` is three element
    values: ``["element1", ["comp", "osite"], "element3"]``.

    The ISA Segment is special. It's generally fixed-length, and it defines the element and segment separators
    used everywhere else.

    When parsing ISA, we handle this as a distinct special case.

    ..   note:: IndexError: pop from empty list

        often means wrong separators.
    """
    _segment_name: str
    _skip_validation: DefaultDict[str, list[str]]
    _helpers: dict[str, X12TypeHelper]
    # No type hint: invisible to get_type_hints() introspection
    logger = logging.getLogger("x12.base.Segment")

    def __init__(self, **arg_dict) -> None:
        fields = get_type_hints(self.__class__, include_extras=True)
        unknowns = arg_dict.keys() - fields.keys()
        if unknowns:
            raise TypeError(f"unknown fields: {unknowns}")
        self.__class__.make_helpers(fields)

        # Ideally all missing names are also not required.
        for name, hint in class_fields(self.__class__):
            # Validate (possibly raising ValueError)
            try:
                self._helpers[name].validate(arg_dict.get(name))
            except ValueError as ex:
                if not (name in self._skip_validation and len(ex.args) == 2):
                    print(f"{self.__class__.__name__} {self._skip_validation=} {name=} {ex.args=}")
                    raise ValueError(f"invalid {self.__class__.__name__}.{name}: {hint} {ex}") from ex
                msg, annotation = ex.args
                if annotation not in self._skip_validation[name]:
                    print(f"{self.__class__.__name__} {self._skip_validation=}")
                    raise ValueError(f"invalid {self.__class__.__name__}.{name}: {hint} {ex}") from ex

            # Set values of these attributes
            value = self._helpers[name].to_py(arg_dict.get(name))
            setattr(self, name, value)

    @classmethod
    def make_helpers(cls, fields: dict[str, type(Any)], instance: Union["Segment", None] = None) -> None:
        """
        Build the element ``_helpers`` for this Segment.

        Used like this ``Segment.make_helpers(fields)``

        ..  todo: remove instance parameter
        """
        if hasattr(cls, '_helpers'):
            return

        cls._helpers = {
            name: X12TypeHelper.annotated(hint)
            for name, hint in class_fields(cls)
        }

        # Propogate down into Composites
        for name, hint in class_fields(cls): # fields.items():
            # TODO: Refactor into recursive walk through type...
            # Or use ``while type(hint) is not type:`` to PEEL THE ONION.
            match hint:
                # TODO: PEEL THE ONION...
                case UnionType():
                    alterative = get_args(hint)[0]
                    # Call recursively get to Composite or primitive
                case GenericAlias() if get_origin(hint)[0] is list:
                    repeating = get_args(hint)[0]
                    # Call recursively to get to Composite or primitive
                case _AnnotatedAlias():
                    base = get_args(hint)[0]
                    # Call recursively to get to Composite or primitive
                case type() if issubclass(hint, Composite):
                    hint.make_helpers(get_type_hints(hint, include_extras=True))
                case type():
                    pass  # No push down into primitives.
                case _:
                    raise ValueError(f"unexpected {name}: {hint} in {cls}")

    @classmethod
    def configure(cls, skip_validation : list[str]) -> None:
        fields = get_type_hints(cls, include_extras=True)
        cls.make_helpers(fields)
        all_rules = (
            rule.split(":")
            for rule in skip_validation
        )
        try:
            segment_rules = [
                (field_match, annotation_name)
                for class_match, field_match, annotation_name in all_rules
                if fnmatch.fnmatch(cls.__name__, class_match)
            ]
        except ValueError:
            raise ValueError(f"invalid skip_validation rule in {skip_validation}")
        cls.logger.debug("Skip Validation: %s", segment_rules)

        cls._skip_validation: DefaultDict[str, list[str]] = defaultdict(list)

        for field_match, annotation_name in segment_rules:
            # Does this match any names in the class?
            # print(f"configure {field_match=} {annotation_name=} in {cls.__name__}")
            for name, helper in cls._helpers.items():
                if fnmatch.fnmatch(name, field_match):
                    cls._skip_validation[name].append(annotation_name)

        # Propogate down to composites.
        for name, field_type in class_fields(cls):
            # TODO: PEEL THE ONION
            while type(field_type) is not type:
                if isinstance(field_type, _AnnotatedAlias):
                    # Annotated[f, ...]
                    field_type = get_args(field_type)[0]
                if isinstance(field_type, GenericAlias):
                    # List[f]
                    field_type = get_args(field_type)[0]
            # print(f"configure {field_type}, {type(field_type)}")
            if issubclass(field_type, Composite):
                field_type.composite_configure(cls._skip_validation, segment_rules, name)

    @classmethod
    def attr_build(cls, field_type: type, source_values: list[str | None | list[str | None]]) -> "Segment":
        """
        Walks type structure to create Elements and Composites.

        May pop source_values.
        """
        cls.logger.debug("attr_build %s from %r", field_type, source_values)
        match field_type:
            case GenericAlias() if get_origin(field_type) is list: # list[Something]
                # COMPOSITE-LIKE PROCESSING for elements that are list[Element]
                repeating_type = get_args(field_type)[0]
                cls.logger.debug("  parse array list[%s]", repeating_type.__name__)
                source_array = cast(list[str], source_values.pop(0))
                values = [repeating_type.build(item) for item in source_array]
                return values
            case UnionType():  # list[Something] | None OR Something | None
                alternatives = list(filter(lambda t: t is not NoneType, get_args(field_type)))
                if len(alternatives) != 1:
                    raise NotImplementedError(f"not supported {field_type}; too many {alternatives}")  # pragma: no cover
                if source_values:
                    return cls.attr_build(alternatives[0], source_values)
                return cls.attr_build(alternatives[0], [None])
            case _AnnotatedAlias():  # type: ignore [misc]
                # Consume a single value from the elements of this segment.
                # Either a ``Composite`` or list or some primitive type
                base = get_args(field_type)[0]
                value = cls.attr_build(base, source_values)
                return value
            case type() if issubclass(field_type, Composite):
                # Next value in this segment *should* be a list.
                # If it didn't have the component separator, we make single-element list.
                composite_value: list[str | None]
                if source_values:
                    composite_value = cast(list[str | None], source_values.pop(0))
                else:
                    composite_value = []
                # print(f"Segment.attr_build() {field_type} with {composite_value}")
                value = field_type.build(composite_value)
                return value
            case type():
                if source_values:
                    value = source_values.pop(0)
                    return value
                return None
        raise TypeError(f"unexpected type {field_type} {type(field_type)} in {cls}")  # pragma: no cover

    @classmethod
    def parse(cls: type["Segment"], source: Source) -> Union["Segment", None]:
        """
        Parses segments in general.

        Handles ISA segments specially to extract element and segment separators.
        """
        fields = get_type_hints(cls, include_extras=True)
        cls.logger.debug("parse %s with %s", cls.__name__, fields)
        if source.next_segment().upper() != cls._segment_name.upper():
            return None

        # Special case: the ISA segment can update the ``Source`` parsing state.
        # Used in the general case when ISA header not compressed.
        if cls._segment_name.upper() == "ISA" and not source.element_sep:
            # Sum of field size + separator for each field.
            # + 3 for header "ISA"
            # + 1 for final segment separator
            args: list[list[X12Annotation]] = [get_args(hint)[1:] for name, hint in class_fields(cls)]
            max_len_params: list[int] = [
                ann.params[0]
                for arg in args
                if arg
                    for ann in arg
                    if isinstance(ann, MaxLen)
            ]
            # print(f"Segment.parse() {max_len_params}")
            size = sum(p+1 for p in max_len_params) + 4
            cls.logger.debug("ISA: source.element_sep=%r, source.segment_sep=%r", source.element_sep, source.segment_sep)
            cls.logger.debug("ISA: size=%s", size)  # Should be 106 to include segment separator
            raw_text = source.peek(size)
            cls.logger.debug("ISA: text=%r, separators text[-3:]=%r", raw_text, raw_text[-3:])
            # ISA16 has the composite separator.
            # It's preceeded by an element separator and followed by the segment separator.
            source.element_sep = raw_text[-3]
            source.array_sep = raw_text[-2]
            source.segment_sep = raw_text[-1]

        flat_values = cast(list[str | None | list[str | None]], source.elements())[1:]  # Drops the segment name from the values.
        cls.logger.debug("SEGMENT %s: fields %s values %r", cls.__name__, fields, flat_values)

        arg_dict: dict[str, Any] = {
            name: cls.attr_build(field_type, flat_values)
            for name, field_type in class_fields(cls)
        }
        obj = cls(**arg_dict)
        return obj

    def elements(self) -> list[str | None | list[str | None]]:
        """
        The segment as a list of Element source values.
        The segment text is the basis for the exchange format: a sequence of Segments.

        .. note:: Confusing overlap of names with the Source class.

            This is **not** the same concept at all.

        ..  todo:: Rename this to not conflict with semantics of `Source`.
        """
        values: list[str | None | list[str | None]] = []
        for name, hint in class_fields(self.__class__):
            field: Composite | list[Composite] = getattr(self, name)
            match field:
                case list():
                    values.append([v.elements() for v in field])
                case Composite():
                    values.append(field.elements())
                case _:
                    # Annotated fields
                    self.logger.debug(f"{name}: {field} {self._helpers[name]} {getattr(self, name)=}")
                    values.append(getattr(self, name))
        return values

    def __repr__(self) -> str:
        good_names = (name for name, hint in class_fields(self.__class__))
        text = ", ".join(f"{name}={getattr(self, name)!r}" for name in good_names)
        return f"{self.__class__.__name__}({text})"


class Loop:
    """
    A Loop is a collection of ``Loop | Segment``.

    Loops and Segments can repeat, and may be optional.

    We need to examine the loop's structure of segments
    and sub-loops to match the segment prefix.
    """
    logger = logging.getLogger("x12.base.Loop")

    def __init__(self, **arg_dict) -> None:
        fields = get_type_hints(self.__class__)
        unknowns = arg_dict.keys() - fields.keys()
        if unknowns:
            raise TypeError(f"unknown fields: {unknowns}")
        # Ideally all missing names are also not required.
        for name, hint in class_fields(self.__class__):
            setattr(self, name, arg_dict.get(name))

    @classmethod
    def configure(cls, skip_validation : list[str]) -> None:
        for name, field_type in class_fields(cls):
            # TODO: PEEL THE ONION...
            while type(field_type) != type:
                match field_type:
                    case GenericAlias() if get_origin(field_type) is list:
                        field_type = get_args(field_type)[0]
                    case UnionType():  # Something | None OR list[Something] | None
                        alternatives = list(filter(lambda t: t is not NoneType, get_args(field_type)))
                        field_type = alternatives[0]
                    case _AnnotatedAlias():
                        field_type = get_args(field_type)[0]
            if issubclass(field_type, Segment):
                field_type.configure(skip_validation)
            elif issubclass(field_type, Loop):
                field_type.configure(skip_validation)
            else:
                raise TypeError(f"unexpected {name}: {field_type} {type(field_type)}")

    @classmethod
    def attr_build(cls, name: str, field_type: type, source: Source) -> Segment | list[Segment] | None:
        """
        Walks type structure to create sub-loops and segments.

        May consume source segments.
        """
        cls.logger.debug("attr_build %s: %r", name, field_type)
        match field_type:
            case GenericAlias() if get_origin(field_type) is list:
                # list[Something]
                repeating_type = get_args(field_type)[0]
                cls.logger.debug("  parse list[%s]", repeating_type.__name__)
                values: list[Segment] = []
                next_value = repeating_type.parse(source)
                while next_value:
                    values.append(next_value)
                    next_value = repeating_type.parse(source)
                return values
            case UnionType():  # Something | None OR list[Something] | None
                alternatives = list(filter(lambda t: t is not NoneType, get_args(field_type)))
                # OR. Try each alternative until one works.
                # For None supply a None instance.
                if len(alternatives) != 1:
                    raise NotImplementedError(f"not supported {field_type}; too many {alternatives}")  # pragma: no cover
                alt_type = alternatives[0]
                return cls.attr_build(name, alt_type, source)
            case _AnnotatedAlias():  # Annotated[base, ...]
                base_type = get_args(field_type)[0]
                return cls.attr_build(name, base_type, source)

            case type() if issubclass(field_type, Segment):
                seg_name = source.next_segment()
                if seg_name == "":
                    return None
                # TODO: segment with usage == "R"? Error
                #  Else move on to next potential segment of Loop?
                if field_type._segment_name.upper() != seg_name.upper():
                    cls.logger.debug("expected field segment_name %r != next segment %r", field_type._segment_name, seg_name)
                    return None
                return field_type.parse(source)

            case type() if issubclass(field_type, Loop):  # pragma: no cover
                raise RuntimeError("unexpected nested Loop: {field_type} inside {cls.__name__}")
                # An alternative is to try to parse it...
                # return field_type.parse(source)

        raise TypeError(f"unexpected {field_type} {type(field_type)}")  # pragma: no cover

    @classmethod
    def parse(cls: type["Loop"], source: Source) -> Union["Loop", None]:
        cls.logger.debug("parse %s", cls.__name__)
        arg_dict = {}
        for name, field_type in class_fields(cls):
            cls.logger.debug("parse() %s: %r", name, field_type)
            # Drill into type annotations. May recursively walk down the annotations
            loop_value = cls.attr_build(name, field_type, source)
            if loop_value:
                arg_dict[name] = loop_value
        if arg_dict:
            return cls(**arg_dict)
        else:
            cls.logger.debug("Skipping %s", cls)
            return None

    def segment_iter(self) -> Iterator[list[str | None | list[str | None]]]:
        """
        A flat sequence of segment values in this Loop and all sub Loops.
        This is the basis for the exchange format.

        This **also** does an ``attr_build()`` style type walk.
        """
        for name, hint in class_fields(self.__class__):
            field = getattr(self, name)
            self.logger.debug("segment_iter() name=%s field=%s", name, field)
            match field:
                case list():  # list[Something]
                    for instance in field:
                        if isinstance(instance, Loop):
                            yield from instance.segment_iter()
                        elif isinstance(instance, Segment):
                            name = cast(Segment, instance)._segment_name
                            yield cast(list[str | None | list[str | None]], [name]) + instance.elements()
                        else:
                            raise TypeError(f"unexpected {name}: {hint} instance {instance=}")  # pragma: no cover
                case Loop(): # _ if isinstance(field, Loop):
                    # This doesn't seem to arise in practice.
                    yield from field.segment_iter()  # pragma: no cover
                case Segment(): # _ if isinstance(field, Segment):
                    name = cast(Segment, field)._segment_name
                    yield cast(list[str | None | list[str | None]], [name]) + field.elements()
                case None:
                    # TypeAlias will have a value of None.
                    pass
                case _:  # pragma: no cover
                    raise TypeError(f"unexpected {name}: {fields[name]} {field=}")

    def __repr__(self) -> str:
        good_names = (n for n, h in class_fields(self.__class__))
        text = ", ".join(f"{name}={getattr(self, name)}" for name in good_names if hasattr(self, name))
        return f"{self.__class__.__name__}({text})"


class Message:
    """
    A top-level message, a collection of Loop.

    Loops can (and often do) repeat.
    """
    # Schema: type[SchemaType]
    logger = logging.getLogger("x12.base.Message")

    def __init__(self, **arg_dict) -> None:
        fields = get_type_hints(self.__class__)
        unknowns = arg_dict.keys() - fields.keys()
        if unknowns:
            raise TypeError(f"unknown fields: {unknowns}")
        # Ideally all missing names are also not required.
        for name, hint in class_fields(self.__class__):
            setattr(self, name, arg_dict.get(name))

    @classmethod
    def configure(cls, skip_validation : list[str]) -> None:
        for name, loop_type in class_fields(cls):
            # PEEL THE ONION.
            while type(loop_type) != type:
                match loop_type:
                    case GenericAlias() if get_origin(loop_type) is list:
                        loop_type = get_args(loop_type)[0]
                    case UnionType():  # Something | None OR list[Something] | None
                        alternatives = list(filter(lambda t: t is not NoneType, get_args(loop_type)))
                        loop_type = alternatives[0]
                    case _AnnotatedAlias():
                        loop_type = get_args(loop_type)[0]
            if issubclass(loop_type, Loop):
                loop_type.configure(skip_validation)
            else:
                raise TypeError(f"unexpected {name}: {loop_type} {type(loop_type)}")

    @classmethod
    def attr_build(cls, name: str, loop_type: type, source: Source) -> Segment | list[Segment] | None:
        match loop_type:
            case GenericAlias() if get_origin(loop_type) is list:
                # list[Loop] structure
                repeating_type = get_args(loop_type)[0]
                values = []
                next_value = repeating_type.parse(source)
                while next_value:
                    values.append(next_value)
                    next_value = repeating_type.parse(source)
                return values
            case _AnnotatedAlias():
                base_type = get_args(loop_type)[0]
                return cls.attr_build(name, base_type, source)
            case type() if issubclass(loop_type, Loop):
                # Stand-alone Loop without a list[Loop] framing...
                value = loop_type.parse(source)
                return value
            case _:
                raise TypeError(f"unexpected {name}: {loop_type} {type(loop_type)}")

    @classmethod
    def parse(cls: type["Message"], source: Source, skip_validation : list[str] | None = None) -> "Message":
        cls.configure(skip_validation or [])

        # Parse the fields of this message.
        cls.logger.debug("parse %s", cls.__name__)
        arg_dict = {}
        for name, loop_type in class_fields(cls):
            cls.logger.debug("parse() %s: %r %s", name, loop_type, type(loop_type))
            arg_dict[name] = cls.attr_build(name, loop_type, source)

        return cls(**arg_dict)

    def segment_iter(self) -> Iterator[list[str | None | list[str | None]]]:
        """
        A flat sequence of segments in a Message.
        This is the basis for the exchange format.
        """
        for name, hint in class_fields(self.__class__):
            loop = getattr(self, name)
            match loop:
                case list():
                    for instance in loop:
                        yield from instance.segment_iter()
                case Loop():
                    yield from loop.segment_iter()
                case _:  # pragma: no cover
                    raise TypeError(f"unexpected {name}: {fields[name]} {loop=}")

    def __repr__(self) -> str:
        good_names = (n for n, h in class_fields(self.__class__))
        text = ", ".join(f"{name}={getattr(self, name)}" for name in good_names)
        return f"{self.__class__.__name__}({text})"

    def __bool__(self):
        return any(getattr(self, name) for name, hint in class_fields(self.__class__))

    def json(self, indent: int | None = None) -> str:
        return json.dumps(asdict(self), indent=indent)


def demo() -> None:
    """
    A minimal demonstration of some features of the base classes.

    >>> from x12.base import demo
    >>> demo()
    MSG(isa_loop=[ISA_LOOP(isa=ISA_LOOP_ISA(isa01=ISA_LOOP_ISA01('00'), isa16=ISA_LOOP_ISA16(':')))])
    """

    ID: TypeAlias = str
    AN: TypeAlias = str

    class ISA_LOOP_ISA(Segment):
        _segment_name = "ISA"
        isa01: Annotated[ID, Title('I01'), MinLen(2), MaxLen(2)]
        isa16: Annotated[AN, Title('I16'), MinLen(1), MaxLen(1)]

    class ISA_LOOP(Loop):
        isa: ISA_LOOP_ISA

    class MSG(Message):
        """Demonstration"""
        isa_loop: list[ISA_LOOP]

    text = "ISA|00|:~"
    logger.info("Building from %r", text)
    parsed_m = MSG.parse(Source(text, element_sep="|", segment_sep="~"))
    print(parsed_m)

    built_m = MSG(
        isa_loop=[
            ISA_LOOP(
                isa=ISA_LOOP_ISA(
                    isa01='00',
                    # etc.
                    isa16=':',
                )
            )
        ]
    )
    print(built_m)

if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    demo()
    logging.shutdown()
