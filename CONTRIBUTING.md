# Contributing

Thanks for your interest in improving `d1-copy`. The bar for a change is
whether it keeps the one promise the tool makes: a copy it calls exact is
exact, and nothing it does can change a database that already held data. A
case where it gets that wrong, a D1 behaviour it does not handle, a sharper
message, and clearer docs are all welcome.

## Reporting a case it gets wrong

Include what you ran, what it printed, and the shape of the schema involved
(the `CREATE TABLE` statements are usually enough). Leave out tokens, account
ids, and any row from a real database: a reproduction with invented rows is
what a test here is built from.

## Running the checks

These are the checks CI runs on every push and pull request:

```sh
python3 -m unittest discover -s tests -t .
python3 -m pip install -r requirements-dev.txt
mypy
```

The tests need no Cloudflare account: they run against local SQLite and a
stand-in for the D1 API (`tests/fake_d1.py`).

## Conventions

- **Tests ship with the change.** A bug fix or a feature includes a test in
  the same pull request. A test that guards a failure mode says which one.
- **Fail loudly.** A comparison that cannot be trusted is a non-zero exit,
  never a pass.
- **Never print a row.** Differences name a table and give counts.
- **Standard library only at runtime.**
- **Pin dependencies exactly.** An action reference is its release's commit
  SHA with the version beside it, since a tag can be moved. The tools in
  `requirements-dev.txt` are exact versions. Renovate proposes their updates.
- **Comments explain why, not what.**
- **Keep tracked text ASCII.**

## Submitting

Keep each pull request focused on one change, make sure the checks pass, and
describe the why in the pull request body. CI must be green before merge.

## Releasing

A patch or minor release needs no approval: whoever cuts it follows the steps
below in order, on a `main` commit whose required checks have passed. A major
version, a 1.0, or a minor before 1.0 that changes an interface consumers use
needs the maintainer's explicit approval before its tag is pushed.

Every release gets a fixed `vMAJOR.MINOR.PATCH` tag. The repository blocks
updates and deletions of those tags, and publishing the GitHub Release makes
the tag immutable.

1. Bump `version` in `pyproject.toml` and merge that to `main`.
2. Tag the merge commit `vMAJOR.MINOR.PATCH` and push the tag.
3. Publish it: `gh release create vMAJOR.MINOR.PATCH --verify-tag`.

## License

By submitting a contribution for inclusion in this project, you agree that it
is licensed under the MIT and Apache-2.0 licenses, as the README's License
section describes, without any additional terms or conditions.
