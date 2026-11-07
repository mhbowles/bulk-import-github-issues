"""Thin wrapper around the GitHub CLI."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time

ISSUE_URL = re.compile(r"https://\S*/issues/\d+")


class GhError(Exception):
    pass


def _run(args, timeout=120):
    try:
        return subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", timeout=timeout
        )
    except FileNotFoundError:
        raise GhError(
            "gh is not installed or not on PATH. Install it with:\n"
            "    winget install --id GitHub.cli"
        )
    except subprocess.TimeoutExpired:
        raise GhError("gh timed out after {}s: {}".format(timeout, " ".join(args)))


def check_available():
    """Verify gh is installed and authenticated before any issues are created."""
    if shutil.which("gh") is None:
        raise GhError(
            "gh is not installed or not on PATH. Install it with:\n"
            "    winget install --id GitHub.cli"
        )
    result = _run(["gh", "auth", "status"])
    if result.returncode != 0:
        raise GhError(
            "gh is installed but not authenticated. Run:\n"
            "    gh auth login\n\n{}".format((result.stderr or result.stdout).strip())
        )


def check_repo(repo):
    result = _run(["gh", "repo", "view", repo, "--json", "name"])
    if result.returncode != 0:
        raise GhError(
            "cannot reach repo {} - check the owner/name spelling and that your "
            "account has access.\n\n{}".format(repo, (result.stderr or "").strip())
        )


def existing_labels(repo):
    """Return the set of label names defined on the repo."""
    result = _run(
        ["gh", "label", "list", "--repo", repo, "--limit", "200", "--json", "name"]
    )
    if result.returncode != 0:
        raise GhError(
            "could not list labels on {}:\n{}".format(repo, (result.stderr or "").strip())
        )
    return {label["name"] for label in json.loads(result.stdout or "[]")}


def create_issue(repo, title, body, labels=(), project=None, retries=2, backoff=5.0):
    """Create one issue and return its URL.

    The body goes through a temp file rather than an argument: reports are
    multi-line and contain quotes and hashes that Windows argument parsing
    would otherwise mangle.
    """
    handle, body_path = tempfile.mkstemp(suffix=".md", text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(body)

        args = ["gh", "issue", "create", "--repo", repo,
                "--title", title, "--body-file", body_path]
        for label in labels:
            args += ["--label", label]
        if project:
            args += ["--project", project]

        last_error = ""
        for attempt in range(retries + 1):
            result = _run(args)
            if result.returncode == 0:
                match = ISSUE_URL.search(result.stdout or "")
                if match:
                    return match.group(0)
                # Exit 0 without a URL means the issue may well exist; stop
                # rather than retrying and risking a duplicate.
                raise GhError(
                    "gh reported success but printed no issue URL:\n{}".format(
                        (result.stdout or "").strip()
                    )
                )
            last_error = (result.stderr or result.stdout or "").strip()
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))

        raise GhError(last_error or "gh issue create failed")
    finally:
        try:
            os.unlink(body_path)
        except OSError:
            pass


def _json_out(args, what):
    """Run a gh command that prints JSON and return the parsed payload."""
    result = _run(args)
    if result.returncode != 0:
        raise GhError("{}:\n{}".format(
            what, (result.stderr or result.stdout or "").strip()))
    try:
        return json.loads(result.stdout or "null")
    except ValueError:
        raise GhError("{}: gh printed something that is not JSON:\n{}".format(
            what, (result.stdout or "").strip()))


def _find_project(owner, title):
    """Return (project_id, number) for the owner's project called title."""
    payload = _json_out(
        ["gh", "project", "list", "--owner", owner,
         "--limit", "100", "--format", "json"],
        "could not list projects owned by {}. Projects need their own auth "
        "scope - run:\n    gh auth refresh -s project".format(owner),
    )
    projects = payload.get("projects", []) if isinstance(payload, dict) else payload
    for project in projects:
        if (project.get("title") or "").strip().lower() == title.strip().lower():
            return project["id"], project["number"]
    known = ", ".join(sorted(
        repr(p.get("title", "")) for p in projects)) or "none"
    raise GhError("{} owns no project called {!r}. Projects found: {}".format(
        owner, title, known))


# Asking the project for its fields by node id works whether the project
# belongs to a user or an organization, which the REST-ish gh subcommands do
# not - they need the owner kind up front.
_FIELDS_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on ProjectV2 {
      fields(first: 50) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id
            name
            options { id name }
          }
        }
      }
    }
  }
}
"""


def _find_option(project_id, field_name, option_name):
    """Return (field_id, field_name, option_id, option_name), matched by name."""
    payload = _json_out(
        ["gh", "api", "graphql", "-f", "query=" + _FIELDS_QUERY,
         "-f", "id=" + project_id],
        "could not read the fields of that project",
    )
    try:
        nodes = payload["data"]["node"]["fields"]["nodes"]
    except (KeyError, TypeError):
        raise GhError("unexpected reply when reading the project's fields")

    # Fields of other kinds match no fragment and come back as empty objects.
    fields = [node for node in nodes if node.get("name")]
    for field in fields:
        if field["name"].strip().lower() == field_name.strip().lower():
            for option in field["options"]:
                if option["name"].strip().lower() == option_name.strip().lower():
                    return field["id"], field["name"], option["id"], option["name"]
            known = ", ".join(repr(o["name"]) for o in field["options"]) or "none"
            raise GhError(
                "the {!r} field has no option called {!r}. Options are: "
                "{}".format(field["name"], option_name, known))
    known = ", ".join(sorted(repr(f["name"]) for f in fields)) or "none"
    raise GhError(
        "that project has no single-select field called {!r}. Single-select "
        "fields are: {}".format(field_name, known))


class ProjectStatus:
    """Sets one single-select project field on each issue as it is created.

    Every id is resolved in __init__, before a single issue exists: a
    misspelled project, field or option should stop the run at the start
    rather than after fifty issues have been opened without a status.
    """

    def __init__(self, owner, project, field, value):
        self.owner = owner
        self.project = project
        self.project_id, self.number = _find_project(owner, project)
        self.field_id, self.field, self.option_id, self.value = _find_option(
            self.project_id, field, value)

    def describe(self):
        return "{} = {!r} on project {!r}".format(
            self.field, self.value, self.project)

    def apply(self, issue_url):
        """Put one issue in the project and set its field value.

        Adding is safe to repeat: GitHub returns the existing item when the
        issue is already on the board, so an issue put there by --project, or
        by an earlier interrupted run, is updated rather than duplicated.
        """
        item = _json_out(
            ["gh", "project", "item-add", str(self.number), "--owner", self.owner,
             "--url", issue_url, "--format", "json"],
            "could not add {} to project {!r}".format(issue_url, self.project),
        )
        item_id = (item or {}).get("id")
        if not item_id:
            raise GhError("gh added {} to the project but printed no item "
                          "id".format(issue_url))

        result = _run(
            ["gh", "project", "item-edit", "--id", item_id,
             "--project-id", self.project_id, "--field-id", self.field_id,
             "--single-select-option-id", self.option_id]
        )
        if result.returncode != 0:
            raise GhError("could not set {} on {}:\n{}".format(
                self.describe(), issue_url,
                (result.stderr or result.stdout or "").strip()))
