"""Compare two versions of a module symbol table.

The goal is to find which AST nodes have externally visible changes, so
that we can fire triggers and re-type-check other parts of the program
that are stale because of the changes.

Only look at detail at definitions at the current module.
"""

from typing import Set, List, TypeVar, Dict, Tuple, Optional, Sequence

from mypy.nodes import (
    SymbolTable, SymbolTableNode, FuncBase, TypeInfo, Var, MypyFile, SymbolNode, MODULE_REF,
    TYPE_ALIAS, UNBOUND_IMPORTED, TVAR
)
from mypy.types import (
    Type, TypeVisitor, UnboundType, TypeList, AnyType, NoneTyp, UninhabitedType,
    ErasedType, DeletedType, Instance, TypeVarType, CallableType, TupleType, TypedDictType,
    UnionType, Overloaded, PartialType, TypeType
)
from mypy.util import get_prefix


def is_identical_type(t: Type, s: Type) -> bool:
    return t.accept(IdenticalTypeVisitor(s))


TT = TypeVar('TT', bound=Type)


def is_identical_types(a: List[TT], b: List[TT]) -> bool:
    return len(a) == len(b) and all(is_identical_type(t, s) for t, s in zip(a, b))


class IdenticalTypeVisitor(TypeVisitor[bool]):
    """Visitor for checking whether two types are identical.

    This may be conservative -- it's okay for two types to be considered
    different even if they are actually the same. The results are only
    used to improve performance, not relied on for correctness.

    Differences from mypy.sametypes:

    * Types with the same name but different AST nodes are considered
      identical.

    * If one of the types is not valid for whatever reason, they are
      considered different.

    * Sometimes require types to be structurally identical, even if they
      are semantically the same type.
    """

    def __init__(self, right: Type) -> None:
        self.right = right

    # visit_x(left) means: is left (which is an instance of X) the same type as
    # right?

    def visit_unbound_type(self, left: UnboundType) -> bool:
        return False

    def visit_any(self, left: AnyType) -> bool:
        return isinstance(self.right, AnyType)

    def visit_none_type(self, left: NoneTyp) -> bool:
        return isinstance(self.right, NoneTyp)

    def visit_uninhabited_type(self, t: UninhabitedType) -> bool:
        return isinstance(self.right, UninhabitedType)

    def visit_erased_type(self, left: ErasedType) -> bool:
        return False

    def visit_deleted_type(self, left: DeletedType) -> bool:
        return isinstance(self.right, DeletedType)

    def visit_instance(self, left: Instance) -> bool:
        return (isinstance(self.right, Instance) and
                left.type.fullname() == self.right.type.fullname() and
                is_identical_types(left.args, self.right.args))

    def visit_type_var(self, left: TypeVarType) -> bool:
        return (isinstance(self.right, TypeVarType) and
                left.id == self.right.id)

    def visit_callable_type(self, left: CallableType) -> bool:
        # FIX generics
        if isinstance(self.right, CallableType):
            cright = self.right
            return (is_identical_type(left.ret_type, cright.ret_type) and
                    is_identical_types(left.arg_types, cright.arg_types) and
                    left.arg_names == cright.arg_names and
                    left.arg_kinds == cright.arg_kinds and
                    left.is_type_obj() == cright.is_type_obj() and
                    left.is_ellipsis_args == cright.is_ellipsis_args)
        return False

    def visit_tuple_type(self, left: TupleType) -> bool:
        if isinstance(self.right, TupleType):
            return is_identical_types(left.items, self.right.items)
        return False

    def visit_typeddict_type(self, left: TypedDictType) -> bool:
        if isinstance(self.right, TypedDictType):
            if left.items.keys() != self.right.items.keys():
                return False
            for (_, left_item_type, right_item_type) in left.zip(self.right):
                if not is_identical_type(left_item_type, right_item_type):
                    return False
            return True
        return False

    def visit_union_type(self, left: UnionType) -> bool:
        if isinstance(self.right, UnionType):
            # Require structurally identical types.
            return is_identical_types(left.items, self.right.items)
        return False

    def visit_overloaded(self, left: Overloaded) -> bool:
        if isinstance(self.right, Overloaded):
            return is_identical_types(left.items(), self.right.items())
        return False

    def visit_partial_type(self, left: PartialType) -> bool:
        # A partial type is not fully defined, so the result is indeterminate. We shouldn't
        # get here.
        raise RuntimeError

    def visit_type_type(self, left: TypeType) -> bool:
        if isinstance(self.right, TypeType):
            return is_identical_type(left.item, self.right.item)
        return False


# Snapshot representation of a symbol table node or type. The representation is
# opaque -- the only supported operations are comparing for equality and
# hashing (latter for type snapshots only). Snapshots can contain primitive
# objects, nested tuples, lists and dictionaries and primitive objects (type
# snapshots are immutable).
#
# For example, the snapshot of the 'int' type is ('Instance', 'builtins.int', ()).
SnapshotItem = Tuple[object, ...]


def compare_symbol_table_snapshots(
        name_prefix: str,
        snapshot1: Dict[str, SnapshotItem],
        snapshot2: Dict[str, SnapshotItem]) -> Set[str]:
    """Return names that are different in two snapshots of a symbol table.

    Return a set of fully-qualified names (e.g., 'mod.func' or 'mod.Class.method').

    Only shallow (intra-module) differences are considered. References to things defined
    outside the module are compared based on the name of the target only.
    """
    # Find names only defined only in one version.
    names1 = {'%s.%s' % (name_prefix, name) for name in snapshot1}
    names2 = {'%s.%s' % (name_prefix, name) for name in snapshot2}
    triggers = names1 ^ names2

    # Look for names defined in both versions that are different.
    for name in set(snapshot1.keys()) & set(snapshot2.keys()):
        item1 = snapshot1[name]
        item2 = snapshot2[name]
        kind1 = item1[0]
        kind2 = item2[0]
        item_name = '%s.%s' % (name_prefix, name)
        if kind1 != kind2:
            # Different kind of node in two snapshots -> trivially different.
            triggers.add(item_name)
        elif kind1 == 'TypeInfo':
            if item1[:-1] != item2[:-1]:
                # Record major difference (outside class symbol tables).
                triggers.add(item_name)
            # Look for differences in nested class symbol table entries.
            assert isinstance(item1[-1], dict)
            assert isinstance(item2[-1], dict)
            triggers |= compare_symbol_table_snapshots(item_name, item1[-1], item2[-1])
        else:
            # Shallow node (no interesting internal structure). Just use equality.
            if snapshot1[name] != snapshot2[name]:
                triggers.add(item_name)

    return triggers


def snapshot_symbol_table(name_prefix: str, table: SymbolTable) -> Dict[str, SnapshotItem]:
    """Create a snapshot description that represents the state of a symbol table.

    The snapshot has a representation based on nested tuples and dicts
    that makes it easy and fast to find differences.

    Only "shallow" state is included in the snapshot -- references to
    things defined in other modules are represented just by the names of
    the targers.
    """
    result = {}  # type: Dict[str, SnapshotItem]
    for name, symbol in table.items():
        node = symbol.node
        # TODO: cross_ref, tvar_def, type_override?
        fullname = node.fullname() if node else None
        common = (fullname, symbol.kind, symbol.module_public)
        if symbol.kind == MODULE_REF:
            # This is a cross-reference to another module.
            assert isinstance(node, MypyFile)
            result[name] = ('Moduleref', common)
        elif symbol.kind == TVAR:
            # TODO: Implement
            assert False
        elif symbol.kind == TYPE_ALIAS:
            # TODO: Implement
            assert False
        else:
            assert symbol.kind != UNBOUND_IMPORTED
            if node and get_prefix(node.fullname()) != name_prefix:
                # This is a cross-reference to a node defined in another module.
                result[name] = ('CrossRef', common)
            else:
                result[name] = snapshot_definition(node, common)
    return result


def snapshot_definition(node: Optional[SymbolNode],
                        common: Tuple[object, ...]) -> Tuple[object, ...]:
    """Create a snapshot description of a symbol table node.

    The representation is nested tuples and dicts. Only externally
    visible attributes are included.
    """
    if isinstance(node, FuncBase):
        # TODO: info
        return ('Func', common, node.is_property, snapshot_type(node.type))
    elif isinstance(node, Var):
        return ('Var', common, snapshot_optional_type(node.type))
    elif isinstance(node, TypeInfo):
        # TODO:
        #   type_vars
        #   bases
        #   _promote
        #   tuple_type
        #   typeddict_type
        attrs = (node.is_abstract,
                 node.is_enum,
                 node.fallback_to_any,
                 node.is_named_tuple,
                 node.is_newtype,
                 [base.fullname() for base in node.mro])
        prefix = node.fullname()
        symbol_table = snapshot_symbol_table(prefix, node.names)
        return ('TypeInfo', common, attrs, symbol_table)
    else:
        # TODO: Handle additional types: TypeVarExpr, MypyFile, ...
        assert False, type(node)


def snapshot_type(typ: Type) -> SnapshotItem:
    """Create a snapshot representation of a type using nested tuples."""
    return typ.accept(SnapshotTypeVisitor())


def snapshot_optional_type(typ: Optional[Type]) -> Optional[SnapshotItem]:
    if typ:
        return snapshot_type(typ)
    else:
        return None


def snapshot_types(types: Sequence[Type]) -> SnapshotItem:
    return tuple(snapshot_type(item) for item in types)


def snapshot_simple_type(typ: Type) -> SnapshotItem:
    return (type(typ).__name__,)


class SnapshotTypeVisitor(TypeVisitor[SnapshotItem]):
    def visit_unbound_type(self, typ: UnboundType) -> SnapshotItem:
        return ('UnboundType',
                typ.name,
                typ.optional,
                typ.empty_tuple_index,
                [snapshot_type(arg) for arg in typ.args])

    def visit_any(self, typ: AnyType) -> SnapshotItem:
        return snapshot_simple_type(typ)

    def visit_none_type(self, typ: NoneTyp) -> SnapshotItem:
        return snapshot_simple_type(typ)

    def visit_uninhabited_type(self, typ: UninhabitedType) -> SnapshotItem:
        return snapshot_simple_type(typ)

    def visit_erased_type(self, typ: ErasedType) -> SnapshotItem:
        return snapshot_simple_type(typ)

    def visit_deleted_type(self, typ: DeletedType) -> SnapshotItem:
        return snapshot_simple_type(typ)

    def visit_instance(self, typ: Instance) -> SnapshotItem:
        return ('Instance',
                typ.type.fullname(),
                snapshot_types(typ.args))

    def visit_type_var(self, typ: TypeVarType) -> SnapshotItem:
        return ('TypeVar',
                typ.name,
                typ.fullname,
                typ.id.raw_id,
                typ.id.meta_level,
                snapshot_types(typ.values),
                snapshot_type(typ.upper_bound),
                typ.variance)

    def visit_callable_type(self, typ: CallableType) -> SnapshotItem:
        # FIX generics
        return ('CallableType',
                snapshot_types(typ.arg_types),
                snapshot_type(typ.ret_type),
                typ.arg_names,
                typ.arg_kinds,
                typ.is_type_obj(),
                typ.is_ellipsis_args)

    def visit_tuple_type(self, typ: TupleType) -> SnapshotItem:
        return ('TupleType', snapshot_types(typ.items))

    def visit_typeddict_type(self, typ: TypedDictType) -> SnapshotItem:
        items = tuple((key, snapshot_type(item_type))
                      for key, item_type in typ.items.items())
        return ('TypedDictType', items)

    def visit_union_type(self, typ: UnionType) -> SnapshotItem:
        # Sort and remove duplicates so that we can use equality to test for
        # equivalent union type snapshots.
        items = {snapshot_type(item) for item in typ.items}
        normalized = tuple(sorted(items))
        return ('UnionType', normalized)

    def visit_overloaded(self, typ: Overloaded) -> SnapshotItem:
        return ('Overloaded', snapshot_types(typ.items()))

    def visit_partial_type(self, typ: PartialType) -> SnapshotItem:
        # A partial type is not fully defined, so the result is indeterminate. We shouldn't
        # get here.
        raise RuntimeError

    def visit_type_type(self, typ: TypeType) -> SnapshotItem:
        return ('TypeType', snapshot_type(typ.item))
