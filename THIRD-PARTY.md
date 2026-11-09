# Third-party notices

The release does not bundle Hermes Agent, GitHub CLI, Python or their dependency
source/binaries. The plugin uses Python standard-library modules. Its tests and
plugin loader require a separately installed **Hermes Agent**:

- Project: Hermes Agent - https://github.com/NousResearch/hermes-agent
- Regression baseline: `c5f347ebe9b840c3dbe5ec99bbe8e36871f24393`
- Additional runtime pin: `0b82ddc96c39449d30763d0fdd50ac22fed0fb31`
- License: MIT. Copyright (c) 2025 Nous Research.

The full MIT text ships with that checkout as `LICENSE` and is preserved there
unmodified. It is reproduced here because this project depends on it at test
time and the notice must travel with the reference:

```
MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Concord is released under MIT. Copyright (c) 2026 Null007Void | Null Foundry.
The upstream notice above is not a claim that Nous Research owns Concord.

The runtime also uses separately installed GitHub CLI (MIT, GitHub, Inc.) and
Python (PSF license). Test tooling uses pytest (MIT, pytest contributors) and
Hermes' own transitive dependencies under their respective licenses. These are
not redistributed by this source package. Obtain their notices with their
installations. No third-party artwork, fonts, recordings, model weights or vendor
libraries are included in this candidate. Dependency presence is not proof of
code ownership; the owner must confirm rights to release the included source.
