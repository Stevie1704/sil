"""Primitive wire facts, also installed beside the dependency-free silschema tool.

Widths come from explicitly little-endian struct formats (never native ABI
sizes); integer bounds follow from those widths. C spellings and the silschema
name rule are the only additional language mapping; sil-schema-import applies
the same rule. The kernel deliberately validates independently;
tests/fixtures/schema_conformance.json pins actual bytes across both languages.
"""

import re
import struct

FORMATS = {
    "u8": "B", "u16": "H", "u32": "I", "u64": "Q",
    "i8": "b", "i16": "h", "i32": "i", "i64": "q",
    "f32": "f", "f64": "d",
}
SIZES = {name: struct.calcsize("<" + fmt) for name, fmt in FORMATS.items()}
INT_RANGES = {
    name: (0, (1 << (size * 8)) - 1) if name.startswith("u") else
          (-(1 << (size * 8 - 1)), (1 << (size * 8 - 1)) - 1)
    for name, size in SIZES.items() if name[0] in "ui"
}
C_TYPES = {
    "u8": "uint8_t", "u16": "uint16_t", "u32": "uint32_t", "u64": "uint64_t",
    "i8": "int8_t", "i16": "int16_t", "i32": "int32_t", "i64": "int64_t",
    "f32": "float", "f64": "double",
}


# The silschema name rule: names that are valid in a C11 and C++17 header.
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
KEYWORDS = frozenset("""
    auto break case char const continue default do double else enum extern
    float for goto if inline int long register restrict return short signed
    sizeof static struct switch typedef union unsigned void volatile while
    _Alignas _Alignof _Atomic _Bool _Complex _Generic _Imaginary _Noreturn
    _Static_assert _Thread_local
    alignas alignof and and_eq asm bitand bitor bool catch char16_t char32_t
    class compl constexpr const_cast decltype delete dynamic_cast explicit
    export false friend mutable namespace new noexcept not not_eq nullptr
    operator or or_eq private protected public reinterpret_cast static_assert
    static_cast template this thread_local throw true try typeid typename
    using virtual wchar_t xor xor_eq
""".split())
# Macros that <stdint.h> defines, or that C reserves for it (C11 7.31.10).
STDINT_MACRO = re.compile(r"(U?INT\w*|SIZE|PTRDIFF|SIG_ATOMIC|WCHAR|WINT)_(MAX|MIN|WIDTH|C)")


def c_ident(schema_name: str) -> str:
    return schema_name.replace(".", "_")


def ident_problem(name) -> str | None:
    """Why `name` cannot be a C and C++ identifier, or None if it can."""
    if not isinstance(name, str) or not IDENT.fullmatch(name):
        return f"does not match {IDENT.pattern}"
    if name in KEYWORDS:
        return "is a C11 or C++17 keyword"
    if "__" in name:
        return "contains '__' (reserved in C and C++)"
    if re.match(r"_[A-Z]", name):
        return "starts with '_' followed by an uppercase letter (reserved in C and C++)"
    if name.endswith("_t"):
        return "ends in '_t' (reserved for type names by POSIX and <stdint.h>)"
    if STDINT_MACRO.fullmatch(name):
        return "is reserved for <stdint.h> macros"
    return None


def schema_name_problem(name: str) -> str | None:
    for segment in name.split("."):
        if problem := ident_problem(segment):
            return f"segment {segment!r} {problem}"
    if problem := ident_problem(c_ident(name)):
        return f"C identifier {c_ident(name)!r} {problem}"
    if name.startswith("_"):
        return "starts with '_' (reserved at file scope in C)"
    return None
