"""Independent bounded Composer layout mapping; not dispatch authority."""
from pathlib import Path

from mcd_agent.mautic_patch_stage import application_root
from mcd_agent.mautic_patch_plan_v3 import _file, _root

SCHEMA = "mcd-mautic-root-mapping-v1"


def discover_root_mapping(selected_root):
    selected = _root(str(selected_root))
    application = application_root(selected)
    project = selected
    if selected.name in {"docroot", "public"} and application == selected:
        if not (selected / "bin/console").exists():
            project = _root(str(selected.parent))
    relative = application.relative_to(project).as_posix()
    if relative not in {".", "docroot", "public"}:
        raise ValueError("root_mapping_layout_invalid")
    candidates = [project / "bin/console"]
    if application != project:
        candidates.append(application / "bin/console")
    consoles = []
    for candidate in candidates:
        if candidate.is_symlink():
            raise ValueError("root_mapping_console_symlink")
        if candidate.is_file():
            console_relative = candidate.relative_to(project).as_posix()
            _file(project, console_relative)
            consoles.append(console_relative)
    if len(consoles) != 1 or selected not in {project, application}:
        raise ValueError("root_mapping_console_ambiguous_or_missing")
    return dict(schema=SCHEMA, project_root=str(project),
        application_root_relative=relative, console_relative_path=consoles[0])
