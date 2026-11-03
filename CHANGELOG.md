# Changelog

## 2.0.0

### Fixed

- Handle both follow and download-only inline buttons without depending on a
  successful callback acknowledgement.
- Skip Bale's documented legacy-client callback acknowledgements and tolerate
  acknowledgement API failures.
- Accept the requested Persian button labels, punctuation/emoji variants,
  Persian half-spaces, Arabic letter variants, and typed yes/no answers.
- Add ordinary reply-keyboard buttons as a fallback and remove them after selection.
- Preserve old inline callback data and migrate existing encrypted state.
- Persist choices before confirmation, make repeated selections idempotent, and
  retain the update offset when processing or storage fails.
- Give visible feedback for expired or unrecognized buttons.
- Detect an active webhook instead of silently polling an unusable queue.
- Replace the obsolete watcher tests and remove the inactive duplicate workflow.

### Improved

- Split transport, interaction routing, keyboards, storage, and archive logic
  into a typed Python package with a backward-compatible `bot.py` entry point.
- Add linting, formatting, Python 3.11/3.12 CI, and offline regression tests.
- Bound polling batches, archive sizes, and state size; validate archive paths.
- Send complete deletion reports in chat-sized chunks.
- Keep active subscriptions enabled when downloading the same repository again.
- Add `/status` and `/cancel`, clearer subscription status, and deployment docs.
