#!/usr/bin/env python3
import argparse
import os
import sys

import libcst as cst
import libcst.matchers as m
from libcst.metadata import MetadataWrapper, PositionProvider


class ActionRequestNoneTransformer(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, fix=False):
        super().__init__()
        self.fix = fix
        self.violations = []
        self.class_actions = set()
        self.actions_with_empty_serializer = set()

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self.class_actions = set()
        self.actions_with_empty_serializer = set()
        for item in node.body.body:
            if isinstance(item, cst.FunctionDef):
                for decorator in item.decorators:
                    if m.matches(
                        decorator.decorator,
                        m.Call(
                            func=m.Name("action") | m.Attribute(attr=m.Name("action"))
                        )
                        | m.Name("action")
                        | m.Attribute(attr=m.Name("action")),
                    ):
                        self.class_actions.add(item.name.value)

            if m.matches(
                item,
                m.SimpleStatementLine(
                    body=[
                        m.Assign(
                            value=m.Name(value="EmptySerializer"),
                        )
                    ]
                ),
            ):
                assign = item.body[0]
                for target in assign.targets:
                    if m.matches(target.target, m.Name()):
                        target_name = target.target.value
                        if target_name.endswith("_serializer_class"):
                            action_name = target_name.replace("_serializer_class", "")
                            self.actions_with_empty_serializer.add(action_name)

    def leave_ClassDef(
        self, original_node: cst.ClassDef, updated_node: cst.ClassDef
    ) -> cst.ClassDef:
        if not self.fix:
            return updated_node

        new_body = []
        for item in updated_node.body.body:
            skip = False
            if m.matches(
                item,
                m.SimpleStatementLine(
                    body=[
                        m.Assign(
                            value=m.Name(value="EmptySerializer"),
                        )
                    ]
                ),
            ):
                assign = item.body[0]
                has_action_serializer = False
                for target in assign.targets:
                    if m.matches(target.target, m.Name()):
                        target_name = target.target.value
                        if target_name.endswith("_serializer_class"):
                            action_name = target_name.replace("_serializer_class", "")
                            if action_name in self.class_actions:
                                has_action_serializer = True
                                break

                if has_action_serializer:
                    skip = True
                    self.violations.append(
                        (
                            0,
                            f"Class {original_node.name.value}: removed serializer assignment",
                        )
                    )

            if not skip:
                new_body.append(item)

        return updated_node.with_changes(
            body=updated_node.body.with_changes(body=new_body)
        )

    def _extract_methods(self, node):
        if isinstance(node, (cst.List, cst.Tuple)):
            methods = []
            for el in node.elements:
                if isinstance(el.value, cst.SimpleString):
                    methods.append(el.value.value.strip("'\""))
            return methods
        if isinstance(node, cst.SimpleString):
            return [node.value.strip("'\"")]
        return []

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        is_action = False
        methods = ["get"]
        extend_schema_decorator = None
        request_none_found = False

        for decorator in original_node.decorators:
            dec = decorator.decorator
            if m.matches(
                dec,
                m.Call(func=m.Name("action") | m.Attribute(attr=m.Name("action")))
                | m.Name("action")
                | m.Attribute(attr=m.Name("action")),
            ):
                is_action = True
                if isinstance(dec, cst.Call):
                    for arg in dec.args:
                        if m.matches(arg, m.Arg(keyword=m.Name("methods"))):
                            methods = self._extract_methods(arg.value)

            if m.matches(
                dec,
                m.Call(
                    func=m.Name("extend_schema")
                    | m.Attribute(attr=m.Name("extend_schema"))
                ),
            ):
                extend_schema_decorator = decorator
                if isinstance(dec, cst.Call):
                    for arg in dec.args:
                        if m.matches(
                            arg, m.Arg(keyword=m.Name("request"), value=m.Name("None"))
                        ):
                            request_none_found = True

        if not is_action or any(m.lower() == "get" for m in methods):
            return updated_node

        has_request_param = (
            len(original_node.params.params) >= 2
            and original_node.params.params[1].name.value == "request"
        )

        if not has_request_param:
            return updated_node

        request_used = False
        for _ in m.findall(original_node.body, m.Name("request")):
            request_used = True
            break

        force_request_none = (
            original_node.name.value in self.actions_with_empty_serializer
        )

        if (not request_used or force_request_none) and not request_none_found:
            pos = self.get_metadata(PositionProvider, original_node).start
            self.violations.append((pos.line, original_node.name.value))

            if self.fix:
                if extend_schema_decorator:
                    call = extend_schema_decorator.decorator
                    if isinstance(call, cst.Call):
                        already_has_request = any(
                            m.matches(arg, m.Arg(keyword=m.Name("request")))
                            for arg in call.args
                        )
                        if not already_has_request:
                            new_args = list(call.args)
                            new_args.insert(
                                0,
                                cst.Arg(
                                    keyword=cst.Name("request"),
                                    value=cst.Name("None"),
                                    equal=cst.AssignEqual(
                                        whitespace_before=cst.SimpleWhitespace(""),
                                        whitespace_after=cst.SimpleWhitespace(""),
                                    ),
                                ),
                            )
                            new_call = call.with_changes(args=new_args)

                            new_decorators = []
                            fixed = False
                            for d in updated_node.decorators:
                                if not fixed and m.matches(
                                    d.decorator,
                                    m.Call(
                                        func=m.Name("extend_schema")
                                        | m.Attribute(attr=m.Name("extend_schema"))
                                    ),
                                ):
                                    new_decorators.append(
                                        d.with_changes(decorator=new_call)
                                    )
                                    fixed = True
                                else:
                                    new_decorators.append(d)
                            return updated_node.with_changes(decorators=new_decorators)
                else:
                    new_decorator = cst.Decorator(
                        decorator=cst.Call(
                            func=cst.Name("extend_schema"),
                            args=[
                                cst.Arg(
                                    keyword=cst.Name("request"),
                                    value=cst.Name("None"),
                                    equal=cst.AssignEqual(
                                        whitespace_before=cst.SimpleWhitespace(""),
                                        whitespace_after=cst.SimpleWhitespace(""),
                                    ),
                                )
                            ],
                        )
                    )
                    return updated_node.with_changes(
                        decorators=[new_decorator] + list(updated_node.decorators)
                    )

        return updated_node


def process_file(filename, fix=False):
    with open(filename) as f:
        code = f.read()

    try:
        module = cst.parse_module(code)
    except Exception as e:
        print(f"Error parsing {filename}: {e}")
        return False

    wrapper = MetadataWrapper(module)
    transformer = ActionRequestNoneTransformer(fix=fix)
    new_module = wrapper.visit(transformer)

    if transformer.violations:
        for line, func in transformer.violations:
            status = "[FIXED]" if fix else "[ERROR]"
            if line > 0:
                print(f"{filename}:{line}: {status} Action '{func}' unused 'request'")
            else:
                print(f"{filename}: {status} {func}")

        if fix:
            with open(filename, "w") as f:
                f.write(new_module.code)
            return True

    return len(transformer.violations) > 0


def main():
    parser = argparse.ArgumentParser(description="Check for unused request in actions")
    parser.add_argument("paths", nargs="+", help="Files or directories to check")
    parser.add_argument("--fix", action="store_true", help="Automatically fix issues")
    args = parser.parse_args()

    exit_code = 0
    for path in args.paths:
        if os.path.isfile(path):
            if process_file(path, fix=args.fix):
                exit_code = 1
        elif os.path.isdir(path):
            for root, _, files in os.walk(path):
                for file in files:
                    if file.endswith(".py"):
                        full_path = os.path.join(root, file)
                        if process_file(full_path, fix=args.fix):
                            exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
