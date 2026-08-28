# Security Policy

## Supported versions

NewsGator is a young, fast-moving self-hosted project. Only the latest commit on
`main` receives security fixes — there are no versioned releases yet. If you run it,
track `main` or pin a recent commit and re-pull regularly.

## Reporting a vulnerability

**Do not open a public issue for security problems.**

Please use GitHub's [private vulnerability reporting](https://github.com/Tekka90/NewsGator/security/advisories/new)
to report a vulnerability. You will get an acknowledgement within a few days.

Things that are in scope and definitely worth reporting:

- Authentication / session-token weaknesses (login, cookie/Bearer handling)
- Authorization bypasses (non-admin reaching admin-only endpoints, cross-user data leaks)
- Anything that could leak configured secrets (`SECRET_KEY`, LLM keys, Readeck token)
- Injection / SSRF / XSS reachable through feed content or the API

## Self-hosting notes

NewsGator is designed to run on your own network or behind your own auth:

- Set a strong, random `SECRET_KEY` (`openssl rand -hex 32`) before first launch.
- The app stores user passwords as salted hashes, but the instance is only as
  private as the network you expose it on — don't expose it to the public internet
  without TLS and an understanding of the attack surface.
- Your `LLM_BASE_URL` server receives the full text of your articles. Point it at a
  server you trust (that's the whole point of self-hosting).
