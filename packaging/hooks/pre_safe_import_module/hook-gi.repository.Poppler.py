"""Poppler is a dynamically provided GI module, not a Python source file."""


def pre_safe_import_module(api):
    api.add_runtime_module(api.module_name)
