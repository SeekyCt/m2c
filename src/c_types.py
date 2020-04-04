"""This file handles variable types, function signatures and struct layouts
based on a C AST. Based on the pycparser library."""

from collections import defaultdict
from typing import Dict, Match, Set, List, Tuple, Optional, Union
import sys
import re

import attr
from pycparser import c_ast as ca
from pycparser.c_ast import ArrayDecl, TypeDecl, PtrDecl, FuncDecl, IdentifierType
from pycparser.c_generator import CGenerator
from pycparser.c_parser import CParser
from pycparser.plyparser import ParseError

from .error import DecompFailure

Type = Union[PtrDecl, ArrayDecl, TypeDecl, FuncDecl]
StructUnion = Union[ca.Struct, ca.Union]
SimpleType = Union[PtrDecl, TypeDecl]


@attr.s
class StructField:
    type: Type = attr.ib()
    name: str = attr.ib()


@attr.s
class Struct:
    fields: Dict[int, List[StructField]] = attr.ib()
    # TODO: bitfields
    size: int = attr.ib()
    align: int = attr.ib()


@attr.s
class Param:
    type: Type = attr.ib()
    name: Optional[str] = attr.ib()


@attr.s
class Function:
    ret_type: Optional[Type] = attr.ib()
    params: Optional[List[Param]] = attr.ib()
    is_variadic: bool = attr.ib()


@attr.s
class TypeMap:
    typedefs: Dict[str, Type] = attr.ib(factory=dict)
    var_types: Dict[str, Type] = attr.ib(factory=dict)
    functions: Dict[str, Function] = attr.ib(factory=dict)
    named_structs: Dict[str, Struct] = attr.ib(factory=dict)
    anon_structs: Dict[int, Struct] = attr.ib(factory=dict)


def to_c(node: ca.Node) -> str:
    return CGenerator().visit(node)


def basic_type(names: List[str]) -> TypeDecl:
    idtype = IdentifierType(names=names)
    return TypeDecl(declname=None, quals=[], type=idtype)


def pointer(type: Type) -> Type:
    return PtrDecl(quals=[], type=type)


def resolve_typedefs(type: Type, typemap: TypeMap) -> Type:
    while (
        isinstance(type, TypeDecl)
        and isinstance(type.type, IdentifierType)
        and len(type.type.names) == 1
        and type.type.names[0] in typemap.typedefs
    ):
        type = typemap.typedefs[type.type.names[0]]
    return type


def pointer_decay(type: Type, typemap: TypeMap) -> SimpleType:
    real_type = resolve_typedefs(type, typemap)
    if isinstance(real_type, ArrayDecl):
        return PtrDecl(quals=[], type=real_type.type)
    if isinstance(real_type, FuncDecl):
        return PtrDecl(quals=[], type=type)
    if isinstance(real_type, TypeDecl) and isinstance(real_type.type, ca.Enum):
        return basic_type(["int"])
    assert not isinstance(
        type, (ArrayDecl, FuncDecl)
    ), "resolve_typedefs can't hide arrays/functions"
    return type


def deref_type(type: Type, typemap: TypeMap) -> Type:
    type = resolve_typedefs(type, typemap)
    assert isinstance(type, (ArrayDecl, PtrDecl)), "dereferencing non-pointer"
    return type.type


def is_void(type: Type) -> bool:
    return (
        isinstance(type, ca.TypeDecl)
        and isinstance(type.type, ca.IdentifierType)
        and type.type.names == ["void"]
    )


def primitive_size(type: Union[ca.Enum, ca.IdentifierType]) -> int:
    if isinstance(type, ca.Enum):
        return 4
    names = type.names
    if "double" in names:
        return 8
    if "float" in names:
        return 4
    if "short" in names:
        return 2
    if "char" in names:
        return 1
    if names.count("long") == 2:
        return 8
    return 4


def function_arg_size_align(type: Type, typemap: TypeMap) -> Tuple[int, int]:
    type = resolve_typedefs(type, typemap)
    if isinstance(type, PtrDecl) or isinstance(type, ArrayDecl):
        return 4, 4
    assert not isinstance(type, FuncDecl), "Function argument can not be a function"
    inner_type = type.type
    if isinstance(inner_type, (ca.Struct, ca.Union)):
        # TODO: This is wrong, we can have a typedef that leads to a anonymous struct
        assert (
            inner_type.name is not None
        ), "Function argument cannot be of anonymous struct type"
        struct = typemap.named_structs.get(inner_type.name)
        assert (
            struct is not None
        ), "Function argument can not be of an incomplete struct"
        return struct.size, struct.align
    size = primitive_size(inner_type)
    return size, size


def var_size_align(type: Type, typemap: TypeMap) -> Tuple[int, int]:
    size, align, _ = parse_struct_member(type, "", typemap)
    return size, align


def is_struct_type(type: Type, typemap: TypeMap) -> bool:
    type = resolve_typedefs(type, typemap)
    if not isinstance(type, TypeDecl):
        return False
    return isinstance(type.type, (ca.Struct, ca.Union))


def get_primitive_list(type: Type, typemap: TypeMap) -> Optional[List[str]]:
    type = resolve_typedefs(type, typemap)
    if not isinstance(type, TypeDecl):
        return None
    inner_type = type.type
    if isinstance(inner_type, ca.Enum):
        return ["int"]
    if isinstance(inner_type, ca.IdentifierType):
        return inner_type.names
    return None


def parse_function(fn: FuncDecl) -> Function:
    params: List[Param] = []
    is_variadic = False
    has_void = False
    if fn.args:
        for arg in fn.args.params:
            if isinstance(arg, ca.EllipsisParam):
                is_variadic = True
            elif isinstance(arg, ca.Decl):
                params.append(Param(type=arg.type, name=arg.name))
            elif isinstance(arg, ca.ID):
                raise DecompFailure(
                    "K&R-style function header is not supported: " + to_c(fn)
                )
            else:
                assert isinstance(arg, ca.Typename)
                if is_void(arg.type):
                    has_void = True
                else:
                    params.append(Param(type=arg.type, name=None))
    maybe_params: Optional[List[Param]] = params
    if not params and not has_void and not is_variadic:
        # Function declaration without a parameter list
        maybe_params = None
    ret_type = None if is_void(fn.type) else fn.type
    return Function(ret_type=ret_type, params=maybe_params, is_variadic=is_variadic)


def parse_constant_int(expr: "ca.Expression") -> int:
    if isinstance(expr, ca.Constant):
        try:
            return int(expr.value.rstrip("lLuU"), 0)
        except ValueError:
            raise DecompFailure(f"Failed to parse {to_c(expr)} as an int literal")
    if isinstance(expr, ca.BinaryOp):
        lhs = parse_constant_int(expr.left)
        rhs = parse_constant_int(expr.right)
        if expr.op == "+":
            return lhs + rhs
        if expr.op == "-":
            return lhs - rhs
        if expr.op == "*":
            return lhs * rhs
        if expr.op == "<<":
            return lhs << rhs
        if expr.op == ">>":
            return lhs >> rhs
    raise DecompFailure(
        f"Failed to evaluate expression {to_c(expr)} at compile time; only simple arithmetic is supported for now"
    )


def get_struct(
    struct: Union[ca.Struct, ca.Union], typemap: TypeMap
) -> Optional[Struct]:
    if struct.name:
        return typemap.named_structs.get(struct.name)
    else:
        return typemap.anon_structs.get(id(struct))


def parse_struct(struct: Union[ca.Struct, ca.Union], typemap: TypeMap) -> Struct:
    existing = get_struct(struct, typemap)
    if existing:
        return existing
    if struct.decls is None:
        raise DecompFailure(f"Tried to use struct {struct.name} before it is defined.")
    ret = do_parse_struct(struct, typemap)
    if struct.name:
        typemap.named_structs[struct.name] = ret
    else:
        typemap.anon_structs[id(struct)] = ret
    return ret


def parse_struct_member(
    type: Type, field_name: str, typemap: TypeMap
) -> Tuple[int, int, Optional[Struct]]:
    type = resolve_typedefs(type, typemap)
    if isinstance(type, PtrDecl):
        return 4, 4, None
    if isinstance(type, ArrayDecl):
        if type.dim is None:
            raise DecompFailure(f"Array field {field_name} must have a size")
        dim = parse_constant_int(type.dim)
        size, align, _ = parse_struct_member(type.type, field_name, typemap)
        return size * dim, align, None
    assert not isinstance(type, FuncDecl), "Struct can not contain a function"
    inner_type = type.type
    if isinstance(inner_type, (ca.Struct, ca.Union)):
        substr = parse_struct(inner_type, typemap)
        return substr.size, substr.align, substr
    # Otherwise it has to be of type Enum or IdentifierType
    size = primitive_size(inner_type)
    return size, size, None


def do_parse_struct(struct: Union[ca.Struct, ca.Union], typemap: TypeMap) -> Struct:
    is_union = isinstance(struct, ca.Union)
    assert struct.decls is not None, "enforced by caller"
    assert struct.decls, "Empty structs are not valid C"

    fields: Dict[int, List[StructField]] = defaultdict(list)
    union_size = 0
    align = 1
    offset = 0
    bit_offset = 0
    for decl in struct.decls:
        if not isinstance(decl, ca.Decl):
            continue
        field_name = f"{struct.name}.{decl.name}"
        type = decl.type

        if decl.bitsize is not None:
            # A bitfield "type a : b;" has the following effects on struct layout:
            # - align the struct as if it contained a 'type' field.
            # - allocate the next 'b' bits of the struct, going from high bits to low
            #   within each byte.
            # - ensure that 'a' can be loaded using a single load of the size given by
            #   'type' (lw/lh/lb, unsigned counterparts). If it straddles a 'type'
            #   alignment boundary, skip all bits up to that boundary and then use the
            #   next 'b' bits from there instead.
            width = parse_constant_int(decl.bitsize)
            ssize, salign, substr = parse_struct_member(type, field_name, typemap)
            align = max(align, salign)
            if width == 0:
                continue
            if ssize != salign or substr is not None:
                raise DecompFailure(f"Bitfield {field_name} is not of primitive type")
            if width > ssize * 8:
                raise DecompFailure(f"Width of bitfield {field_name} exceeds its type")
            if is_union:
                union_size = max(union_size, ssize)
            else:
                if offset // ssize != (offset + (bit_offset + width - 1) // 8) // ssize:
                    bit_offset = 0
                    offset = (offset + ssize) & -ssize
                bit_offset += width
                offset += bit_offset // 8
                bit_offset &= 7
            continue

        if not is_union and bit_offset != 0:
            bit_offset = 0
            offset += 1

        if decl.name is not None:
            ssize, salign, substr = parse_struct_member(type, field_name, typemap)
            align = max(align, salign)
            offset = (offset + salign - 1) & -salign
            fields[offset].append(StructField(type=type, name=decl.name))
            if substr is not None:
                for off, sfields in substr.fields.items():
                    for field in sfields:
                        fields[offset + off].append(
                            StructField(
                                type=field.type, name=decl.name + "." + field.name
                            )
                        )
            if is_union:
                union_size = max(union_size, ssize)
            else:
                offset += ssize
        elif isinstance(type, (ca.Struct, ca.Union)) and type.decls is not None:
            substr = parse_struct(type, typemap)
            if type.name is not None:
                # Struct defined within another, which is silly but valid C.
                # parse_struct already makes sure it gets defined in the global
                # namespace, so no more to do here.
                pass
            else:
                # C extension: anonymous struct/union, whose members are flattened
                align = max(align, substr.align)
                offset = (offset + substr.align - 1) & -substr.align
                for off, sfields in substr.fields.items():
                    for field in sfields:
                        fields[offset + off].append(field)
                if is_union:
                    union_size = max(union_size, substr.size)
                else:
                    offset += substr.size

    if not is_union and bit_offset != 0:
        bit_offset = 0
        offset += 1

    size = union_size if is_union else (offset + align - 1) & -align
    return Struct(fields=fields, size=size, align=align)


def add_builtin_typedefs(source: str) -> str:
    """Add built-in typedefs to the source code (mips_to_c emits those, so it makes
    sense to pre-define them to simplify hand-written C contexts)."""
    typedefs = {
        "u8": "unsigned char",
        "s8": "char",
        "u16": "unsigned short",
        "s16": "short",
        "u32": "unsigned int",
        "s32": "int",
        "u64": "unsigned long long",
        "s64": "long long",
        "f32": "float",
        "f64": "double",
    }
    line = " ".join(f"typedef {v} {k};" for k, v in typedefs.items())
    return line + "\n" + source


def strip_comments(text: str) -> str:
    # https://stackoverflow.com/a/241506
    def replacer(match: Match[str]) -> str:
        s = match.group(0)
        if s.startswith("/"):
            return " " + "\n" * s.count("\n")
        else:
            return s

    pattern = re.compile(
        r'//.*?$|/\*.*?\*/|\'(?:\\.|[^\\\'])*\'|"(?:\\.|[^\\"])*"',
        re.DOTALL | re.MULTILINE,
    )
    return re.sub(pattern, replacer, text)


def parse_c(source: str) -> ca.FileAST:
    try:
        return CParser().parse(source, "<source>")
    except ParseError as e:
        msg = str(e)
        position, msg = msg.split(": ", 1)
        parts = position.split(":")
        if len(parts) >= 2:
            # Adjust the line number by 1 to correct for the added typedefs
            lineno = int(parts[1]) - 1
            posstr = f" at line {lineno}"
            if len(parts) >= 3:
                posstr += f", column {parts[2]}"
            try:
                line = source.split("\n")[lineno].rstrip()
                posstr += "\n\n" + line
            except IndexError:
                posstr += "(out of bounds?)"
        else:
            posstr = ""
        raise DecompFailure(f"Syntax error when parsing C context.\n{msg}{posstr}")


def build_typemap(source: str) -> TypeMap:
    source = add_builtin_typedefs(source)
    source = strip_comments(source)
    ast: ca.FileAST = parse_c(source)
    ret = TypeMap()

    for item in ast.ext:
        if isinstance(item, ca.Typedef):
            ret.typedefs[item.name] = item.type
        if isinstance(item, ca.FuncDef):
            assert item.decl.name is not None, "cannot define anonymous function"
            assert isinstance(item.decl.type, FuncDecl)
            ret.functions[item.decl.name] = parse_function(item.decl.type)
        if isinstance(item, ca.Decl) and isinstance(item.type, FuncDecl):
            assert item.name is not None, "cannot define anonymous function"
            ret.functions[item.name] = parse_function(item.type)

    defined_function_decls: Set[ca.Decl] = set()

    class Visitor(ca.NodeVisitor):
        def visit_Struct(self, struct: ca.Struct) -> None:
            if struct.decls is not None:
                parse_struct(struct, ret)

        def visit_Union(self, union: ca.Union) -> None:
            if union.decls is not None:
                parse_struct(union, ret)

        def visit_Decl(self, decl: ca.Decl) -> None:
            if decl.name is not None:
                ret.var_types[decl.name] = decl.type
            if not isinstance(decl.type, FuncDecl):
                self.visit(decl.type)

        def visit_Enum(self, enum: ca.Enum) -> None:
            if enum.name is not None:
                ret.typedefs[enum.name] = basic_type(["int"])

        def visit_FuncDef(self, fn: ca.FuncDef) -> None:
            if fn.decl.name is not None:
                ret.var_types[fn.decl.name] = fn.decl.type

    Visitor().visit(ast)
    return ret


def set_decl_name(decl: ca.Decl) -> None:
    name = decl.name
    type = decl.type
    while not isinstance(type, TypeDecl):
        type = type.type
    type.declname = name


def type_to_string(type: Type) -> str:
    if isinstance(type, TypeDecl) and isinstance(type.type, (ca.Struct, ca.Union)):
        su = "struct" if isinstance(type.type, ca.Struct) else "union"
        return type.type.name or f"anon {su}"
    else:
        decl = ca.Decl("", [], [], [], type, None, None)
        set_decl_name(decl)
        return to_c(decl)


def dump_typemap(typemap: TypeMap) -> None:
    print("Variables:")
    for var, type in typemap.var_types.items():
        print(f"{var}:", type_to_string(type))
    print()
    print("Functions:")
    for name, fn in typemap.functions.items():
        if fn.params is None:
            params_str = ""
        else:
            params = [type_to_string(arg.type) for arg in fn.params]
            if fn.is_variadic:
                params.append("...")
            params_str = ", ".join(params) or "void"
        ret_str = "void" if fn.ret_type is None else type_to_string(fn.ret_type)
        print(f"{name}: {ret_str}({params_str})")
    print()
    print("Structs:")
    for name, struct in typemap.named_structs.items():
        print(f"{name}: size {struct.size}, align {struct.align}")
        for offset, fields in struct.fields.items():
            print(f"  {offset}:", end="")
            for field in fields:
                print(f" {field.name} ({type_to_string(field.type)})", end="")
            print()
    print()
