# Contributing to dev-team-agents

> If you are planning, writing, or reviewing code, read the [Code Guidelines](#code-guidelines) section below.

Thank you for your interest in contributing to agent-plugins! This project provides plug-ins that encapsulate orchestrated sets of agents and skills, e.g. for a simulated development team.

## How to Contribute

- **Open an Issue First:** Please open an issue before submitting code. Describe the bug or
  feature so it can be discussed before work begins.
- **Development Workflow:**
  - Fork the repository and create a feature branch for your changes.
  - Ensure all unit tests pass before submitting a pull request.
  - Code reviews are required before merging (self-review is acceptable for solo developers).
- **Coding Standards:**
  - Follow the code style defined in `.editorconfig` (UTF-8, LF line endings, 4-space indent for Python).
  - Write clear, maintainable, and well-documented code.
- **Testing:**
  - Add or update unit tests as appropriate for your changes.
  - All tests must pass before your pull request will be considered.
- **Documentation:**
  - Architecture and design notes are stored alongside implementations using `_doc_*.md` filenames
    so they surface at the top of each folder.
  - Every documentation file must include a one-line description of what the file covers, that starts with `Summary:`. Find documentation topics quickly with: `grep -rl "^Summary:" src test --include="_doc_*.md"`.
  - Living documentation files should:
    - Focus on high-level architecture, design intent, and non-obvious decisions.
    - Avoid implementation details that are likely to change; refer to source code for specifics.
    - Be LLM-friendly, using clear language and structure to assist coding agents and future contributors.
  - When designs are updated, documents should be updated to match.
  - When new subsystems are added, they should include a documentation file.
- **Commit Messages:**
  - Use clear, descriptive commit messages that explain what and why you changed.
  - No formal convention is required, but clarity is appreciated.
- **Supported Platforms:**
  - Python 3.10 or later, cross-platform. CI runs on Linux (GitHub Actions default).
- **Contact:**
  - For questions or support, open an issue and @jodavis.

---

## Code Guidelines

### Testing

#### Naming

`test_<class_or_module>_<scenario>_<expected_result>`

Example: `test_dev_team_resume_restores_state_from_context_file`

#### Structure

Use AAA (Arrange–Act–Assert). Use pytest fixtures for shared setup. Group repeated mock
configuration into `expect_<call>` helper functions.

#### Fixtures

Define shared state and dependencies as `@pytest.fixture` functions. Prefer function-scope
fixtures; use module or session scope only when setup cost is high and the fixture is
genuinely stateless across tests.

#### Mocks

- Use `unittest.mock.MagicMock` (or `pytest-mock`'s `mocker` fixture).
- Always pass `spec=` so that accessing an attribute or method not present on the real object
  raises `AttributeError` immediately.
- Wrap each `mock.patch` / `mock_obj.method.side_effect` assignment in an
  `expect_<call>(mock, ...)` helper method for readability and resilience to interface changes.

#### `make_sut()`

Always define a `make_sut()` helper that constructs the subject under test. When the
constructor gains a new dependency, only `make_sut()` needs to change.

#### Async / coroutine patterns

Use `pytest-asyncio` for async tests. Mark tests with `@pytest.mark.asyncio`.

**Simulating incomplete and faulted coroutines:**
- Use `asyncio.Future()` to represent a coroutine that stays incomplete until you decide.
  - Resolve it later: `future.set_result(value)`
  - Fault it: `future.set_exception(SomeException(...))`
  - Leave it pending to assert the caller stays suspended.
- Set a mock's `return_value` to a future to make `await mock.method()` block:
  ```python
  future = asyncio.get_event_loop().create_future()
  mock_dep.fetch.return_value = future
  ```
- Trigger cancellation with `task.cancel()` — Python injects `CancelledError` at the current
  or next `await` point in the running coroutine.

**For every async method on a dependency, cover all of the following scenarios:**

- Coroutine returns normally → caller continues past the `await`
- Coroutine awaits an incomplete `Future` → caller is suspended (assert the returned task is not done)
- Incomplete `Future` then resolved (`future.set_result(x)`) → caller resumes
- `task.cancel()` called while awaiting → `CancelledError` is raised in caller → task ends cancelled
- `task.cancel()` called, but dependency's `Future` completes first → `CancelledError` is
  delivered at the caller's next `await` → task still ends cancelled
- `task.cancel()` called, dependency's `Future` stays incomplete → cancellation propagates to
  the `Future`; caller stays suspended until cancellation is processed
- Dependency raises synchronously (no coroutine returned) → exception propagates to caller
- Dependency's `Future` faulted (`future.set_exception(e)`) → exception propagates to caller

Assert task state without `await` using the task's `.done()`, `.cancelled()`, and
`.exception()` methods, or `asyncio.wait_for` with a zero timeout to step the event loop
without blocking the test indefinitely.

### Documentation

- Architecture and design notes live in `_doc_*.md` files alongside the code they describe.
- Find documentation topics: `grep -rl "^Summary:" --include="_doc_*.md" .`
- Living docs focus on high-level architecture, design intent, and non-obvious decisions.
- Avoid documenting implementation details likely to change; point to source code instead.
- Update docs when designs change; add a doc file when new subsystems are added.

### Async and Subprocess Patterns

- Fetch async-backed data up front before entering processing-heavy code. Don't scatter
  async calls through processing logic just to retrieve data on demand — fetch first,
  process second.
- Always pass a `timeout` argument to `subprocess.run` / `subprocess.Popen` calls. Timeouts should be long enough to allow for unexpected delays, but should detect and halt a process that's likely never going to complete.
- Never swallow `subprocess.CalledProcessError` silently — let it propagate or log it
  explicitly with enough context to diagnose.

### Project Layout

Before creating new files, read `_doc_Projects.md` for the overall repository structure, then
read any `_doc_*.md` files in the target directory to confirm the correct folder for each new file.

---

## Code of Conduct

This project follows the
[Contributor Covenant Code of Conduct](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).
By participating, you are expected to uphold this code.

---

Thank you for helping make agent-plugins better!
