# Harness Runtime Images

`just q4-packaging-check` builds every workspace wheel and exports `requirements.lock.txt`
from `uv.lock`. Use that generated dist directory as the Docker build context:

```bash
uv build --all-packages --wheel --clear --no-create-gitignore --out-dir dist
uv export --all-packages --no-dev --locked --format requirements.txt \
  --no-emit-project --no-emit-workspace --output-file dist/requirements.lock.txt

docker build -f deploy/images/harness-runtime.Dockerfile --target self-hosted-daemon -t arhugula/harness:self-hosted dist
docker build -f deploy/images/harness-runtime.Dockerfile --target managed-cloud-daemon -t arhugula/harness:managed-cloud dist
docker build -f deploy/images/harness-runtime.Dockerfile --target sandbox-runner -t arhugula/harness:sandbox-runner dist
```

The image recipe installs third-party dependencies from the hashed requirements
export, then installs workspace wheels from the local wheelhouse with `--no-deps`.
That keeps package build proof, dependency pinning, and image install proof on the
same artifacts.
