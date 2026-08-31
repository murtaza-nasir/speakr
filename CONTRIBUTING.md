# Contributing to Speakr

Thank you for your interest in contributing to Speakr! We appreciate your time and effort in helping improve this project.

## Ways to Contribute

There are many ways to contribute to Speakr:

- **Report Bugs**: [Open an issue](https://github.com/murtaza-nasir/speakr/issues) describing the problem
- **Suggest Features**: [Start a discussion](https://github.com/murtaza-nasir/speakr/discussions) about your idea
- **Improve Documentation**: Help us make our docs clearer and more comprehensive
- **Translate**: Help translate Speakr into more languages
- **Sponsor**: Support the project financially to enable continued development

## Code Contributions

We welcome code contributions! However, due to the dual-licensing nature of Speakr (AGPLv3 and Commercial), all code contributions are subject to our Contributor License Agreement (CLA).

### Contributor License Agreement (CLA)

Speakr is dual-licensed under:
1. **AGPLv3** - Open source license for the community
2. **Commercial License** - For organizations that cannot comply with AGPLv3

The CLA allows us to:
- Accept your valuable contributions
- Include them in both the open source and commercial versions
- Maintain flexibility to update licenses if needed in the future
- Protect the project from legal issues

**Important**: You retain copyright ownership of your contribution. The CLA simply grants us permission to use it.

### Accepting the CLA

**By submitting a pull request to this repository, you agree to the terms of our [Contributor License Agreement](CLA.md).**

Please review the [CLA document](CLA.md) before submitting your contribution. When you open a PR, our bot will post a reminder about the CLA terms.

### Contribution Process

1. **Fork** the repository
2. **Create a branch** for your feature: `git checkout -b feature/my-awesome-feature`
3. **Make your changes** following our coding standards
4. **Test your changes** thoroughly
5. **Commit** with clear, descriptive messages (see our commit policy below)
6. **Push** to your fork: `git push origin feature/my-awesome-feature`
7. **Open a Pull Request** with a clear description of your changes
8. **Respond to feedback** from maintainers

### Coding Standards

- Follow the existing code style (Python PEP 8 for backend, Vue 3 conventions for frontend)
- Write clear, descriptive commit messages (see below)
- Include comments for complex logic
- Test your changes before submitting
- Keep PRs focused on a single feature or fix

### Database Migrations

Speakr has no migration framework. Schema changes are hand-written in
`src/init_db.py` and run on every startup, against SQLite (the shipped default) and
PostgreSQL (common in larger deployments). Both have to keep working, and a migration
that misbehaves runs on databases full of other people's recordings, so this area has
stricter rules than the rest of the codebase.

**Use the helpers in `src/utils/database.py`, never raw DDL.** Each one alters a single
thing and handles the differences between the two backends, including boolean defaults,
type names and quoting of reserved words such as `user`.

| Need | Helper |
|---|---|
| Add a column | `add_column_if_not_exists()` |
| Relax `NOT NULL` | `drop_not_null()` |
| Change a column type | `migrate_column_type()` |
| Add an index | `create_index_if_not_exists()` |

**Never rebuild a table from a written-out schema.** A migration that does
`CREATE TABLE x_new (...)`, copies the rows, drops the original and renames has frozen
that column list at the moment it was written. Every column added afterwards is
silently destroyed the day the migration runs. Change the one column you care about
instead, which is what the helpers do.

**Make it idempotent.** Migrations run several times per container start, some of them
concurrently: from the entrypoint, from the admin user script, and once per worker
process. A `migration_lock()` keeps them from interleaving, but it fails open after a
timeout rather than holding the container down, so every migration must still recognise
that its work is already done and return without acting.

**Register genuinely one-shot work instead of re-detecting it.** A data fix that can
only ever be needed once, or whose "has this run?" check would scan a whole table on
every boot, belongs in the ledger:

```python
run_once(engine, '0002_short_description_of_the_change', my_migration, logger=app.logger)
```

It runs the callable the first time only and records the id in `schema_migrations`. A
failure is left unrecorded, so it is retried on the next startup. Number ids in
sequence and never reuse or renumber one, since existing installations have the old id
recorded.

**Make failure recoverable.** pysqlite does not open a transaction for DDL, so a bare
`CREATE TABLE` or `ALTER TABLE` commits immediately even inside a
`with engine.connect()` block, and a failure later in the sequence leaves it behind
permanently. If a migration writes more than one statement, drive the transaction
yourself; `drop_not_null()` shows the pattern.

Migrations run in named `_migration_section(...)` groups, so one failure aborts only
its own group and the rest still apply; every failed section is named in an error
banner and recorded in `app.config['MIGRATION_FAILED']`. Put a new migration inside
the section it belongs to (or add a section), never outside all of them.

**Add a column before anything reads it.** Migrations execute top to bottom in one
function, so referencing a column that is added further down fails on precisely the
upgrade path the migration exists for.

**Test the upgrade, not just the result.** `tests/test_upgrade_path.py` takes real
schemas from older releases, seeds them, runs the current migrations over them, and
asserts that no column, index or value was lost and that the schema ends up matching
today's models. Every other test in the suite runs against a database built fresh from
current models, which is the one shape no upgrading user ever has.

The fixtures live in `tests/fixtures/schemas/` as plain SQL, dumped from each tag's own
models, so the tests need no old dependencies and no network. Add a version with:

```bash
python scripts/generate_schema_fixtures.py v0.9.0-alpha
```

Verify with:

```bash
python -m pytest -q tests/test_migration_compatibility.py tests/test_upgrade_path.py \
    tests/test_migration_drop_not_null.py tests/test_migration_infrastructure.py

# and against a real PostgreSQL, which the tests skip without one
docker compose -f docker-compose.postgres.yml up -d postgres
TEST_DATABASE_URI=postgresql://speakr:speakr@localhost:5432/speakr python -m pytest -q tests/
```

These run in CI, and the pre-commit hook runs them whenever you touch `src/init_db.py`,
`src/utils/database.py` or `src/models/`.

A migration needs a test asserting on what a bad one would take away, meaning the other
columns, the indexes and the row values, not merely that the intended change happened.
Values matter as much as schema: when the #379 migration destroyed 27 columns, the
`add_column_if_not_exists()` calls further down the same function immediately put the
names back, so only the emptied values revealed the damage.

### Commit Message Guidelines

Follow the format used in the project:

```
Brief description of what was done

Optional longer explanation if needed
```

**Good examples:**
- `Add inline transcript editing in speaker identification modal`
- `Fix undefined handle_openai_api_error function call in summary error handler`
- `Optimize recording view for mobile with compact layout`

**Avoid:**
- `Fixed bug`
- `Update`
- `Changes`

### Pull Request Guidelines

- Keep PRs focused on a single feature or bug fix
- Reference related issues: `Fixes #123` or `Relates to #456`
- Provide clear description of what changed and why
- Include screenshots for UI changes
- Ensure all tests pass (if applicable)
- Be responsive to review feedback

## Development Setup

See [CLAUDE.md](CLAUDE.md) for detailed development setup instructions.

### Quick Start

```bash
# Clone your fork
git clone https://github.com/YOUR-USERNAME/speakr.git
cd speakr

# Set up development environment
docker-compose -f docker-compose.dev.yml up -d --build

# Or for local development
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
python src/app.py --debug
```

## What Happens After You Submit a PR?

1. **CLA Reminder**: Our bot posts a reminder about the CLA terms (by submitting, you've accepted them)
2. **Automated Tests**: CI/CD pipeline runs (if configured)
3. **Code Review**: Maintainers review your code
4. **Feedback**: You may be asked to make changes
5. **Merge**: Once approved, we merge your PR!

## Other Ways to Help

There are many ways to contribute without code:

- **Bug Reports**: Detailed bug reports are incredibly valuable
- **Feature Requests**: Share your ideas and use cases
- **Documentation**: Typo fixes, clarifications, examples
- **Translations**: Help translate the UI
- **Community Support**: Help others in discussions and issues
- **Spread the Word**: Blog posts, social media, talks about Speakr

## Questions?

- **General Questions**: [GitHub Discussions](https://github.com/murtaza-nasir/speakr/discussions)
- **Bug Reports**: [GitHub Issues](https://github.com/murtaza-nasir/speakr/issues)

## Code of Conduct

Be respectful, inclusive, and professional. We're all here to build something great together.

- Be kind and courteous
- Respect differing viewpoints
- Accept constructive criticism gracefully
- Focus on what's best for the community
- Show empathy towards others

Violations may result in being blocked from contributing.

## License

By contributing to Speakr, you agree that your contributions will be licensed under the project's dual-license model (AGPLv3 and Commercial), as specified in the [CLA](CLA.md).

---

**Thank you for contributing to Speakr!** 🎉
