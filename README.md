# git-caching-proxy

`git-caching-proxy` is a simple read-through cache for git.

Why? If you need to mirror a large set of git repos (that aren't necessarily known in advance), just point your client at `git-caching-proxy` using git `insteadOf` rules and the repos will be fetched / updated on-demand.

## Running

1. Install dependencies with `pip install -r requirements.txt`.
2. Run via `uvicorn git_proxy:app`.

Then fetch a repo from `git-caching-proxy` like `git clone http://localhost:8000/github.com/GrahamDennis/git-caching-proxy.git`. Behind the scenes this will perform `git clone --bare git@github.com:GrahamDennis/git-caching-proxy.git var/data/github.com/GrahamDennis/git-caching-proxy.git` and serve the results from the local clone. Only the requested branches/tags will be fetched from the upstream repo.

## Configuring

Modify `config.yaml` to configure what git servers are supported (instead of / in addition to github.com) and how you would like to connect to the upstreams.

Example:

```
namespaces:
  # map from git-caching-proxy URL prefix to the prefix of the upstream git repo.
  # For example with the config below `/ssh-github.com/GrahamDennis/git-caching-proxy.git` fetches from `ssh://git@github.com/GrahamDennis/git-caching-proxy.git` and will be stored locally in `var/data/ssh-github.com/GrahamDennis/git-caching-proxy.git`,
  # and `/https-github.com/GrahamDennis/git-caching-proxy.git` fetches from `https://github.com/GrahamDennis/git-caching-proxy.git` and will be stored locally in `var/data/https-github.com/GrahamDennis/git-caching-proxy.git`.
  ssh-github.com: 'ssh://git@github.com'
  https-github.com: 'https://github.com'
```
