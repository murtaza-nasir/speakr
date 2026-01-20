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
