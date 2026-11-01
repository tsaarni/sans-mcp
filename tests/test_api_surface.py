import inspect
from pathlib import Path

import pytest

import sans_mcp


def _extract_api_surface() -> str:
    lines: list[str] = []

    for name in sorted(sans_mcp.__all__):
        obj = getattr(sans_mcp, name)
        if inspect.isfunction(obj):
            sig = inspect.signature(obj)
            lines.append(f"def {name}{sig}")
        elif inspect.isclass(obj):
            if issubclass(obj, Exception):
                base_names = ", ".join(b.__name__ for b in obj.__bases__)
                lines.append(f"class {name}({base_names})")
            else:
                lines.append(f"class {name}:")
                annotations = getattr(obj, "__annotations__", {})
                for attr_name, attr_type in sorted(annotations.items()):
                    if not attr_name.startswith("_"):
                        type_name = getattr(attr_type, "__name__", str(attr_type))
                        lines.append(f"    {attr_name}: {type_name}")

                for member_name, member_val in sorted(inspect.getmembers(obj)):
                    if member_name.startswith("_"):
                        continue
                    if inspect.isfunction(member_val) or inspect.ismethod(member_val):
                        sig = inspect.signature(member_val)
                        lines.append(f"    def {member_name}{sig}")

    return "\n".join(lines) + "\n"


def test_public_api_surface(pytestconfig):
    for name, obj in inspect.getmembers(sans_mcp):
        if (
            (inspect.isfunction(obj) or inspect.isclass(obj))
            and getattr(obj, "__module__", None) == "sans_mcp"
            and name not in sans_mcp.__all__
        ):
            assert name.startswith("_"), (
                f"Internal symbol '{name}' is not in __all__ and must be prefixed with '_'"
            )

    update = pytestconfig.getoption("--update-api")
    surface_file = Path(__file__).resolve().parent.parent / "etc" / "api_surface.txt"
    current_surface = _extract_api_surface()

    if update or not surface_file.exists():
        surface_file.parent.mkdir(parents=True, exist_ok=True)
        surface_file.write_text(current_surface)
        if update:
            pytest.skip("Updated public API surface snapshot in etc/api_surface.txt")

    expected_surface = surface_file.read_text()
    assert current_surface == expected_surface, (
        "Public API surface mismatch detected!\n"
        "If this change is intentional, run 'uv run pytest --update-api' to update the snapshot."
    )
