# TODO: optional ARM64 (Graviton) for ecs-express channels

**Status: blocked on CloudFormation/CDK.** Checked 2026-10-01.

ECS Express Mode supports ARM64 (AWS announcement 2026-09-18) and the ECS API
takes `cpuArchitecture` on `create`/`update-express-gateway-service`
(botocore 1.43.106). But `AWS::ECS::ExpressGatewayService` has no such property
in any region checked (eu-west-1, us-east-1, us-west-2, eu-central-1), and
`aws-cdk-lib` 2.272.0 (latest on PyPI) has nothing for it on
`CfnExpressGatewayService`. Our stacks go through CloudFormation, so we cannot
use it yet.

Why no workaround: an arm64 image deployed through the current stack starts on
x86 and fails (exec format error), so the stack create never reaches steady
state. A post-deploy `update-express-gateway-service` call is too late. A
multi-arch image needs the emulated amd64 build anyway, which removes the
benefit.

## Why bother

- Native arm64 image builds on Apple Silicon hosts, instead of the emulated
  `linux/amd64` build (~150s cold, dominated by the gpac source compile).
  Only matters when the image tag is not already in ECR.
- Fargate Graviton is ~20% cheaper per vCPU/GB (small in absolute terms for
  one 256/512 task running 24/7).
- Not a win for x86 build hosts or x86 CI (they would pay emulation instead),
  so the architecture must be a setting, not a hardcoded switch.

## Already verified (2026-10-01)

`loop-dee-loop/Dockerfile` builds for `linux/arm64` natively and runs: image
`Architecture: arm64`, `MP4Box`, `ffmpeg` and `import serve` all work. The
"Couldn't find any modules in lib path /usr/local/lib/gpac" warning from
MP4Box is also present on the x86 image, so it is not new. Not measured: a
cold native build time (the layers were cached).

## Unblock check

Re-run now and then; when it prints a non-zero count, start:

```
aws cloudformation describe-type --region eu-west-1 --type RESOURCE \
  --type-name AWS::ECS::ExpressGatewayService --query Schema --output text \
  | grep -c CpuArchitecture
```

and `python -c "from aws_cdk import aws_ecs as e; import inspect; print(inspect.signature(e.CfnExpressGatewayService.__init__))"`
after `uv sync --upgrade-package aws-cdk-lib` in `its-a-live/`.

## Work, once unblocked

Setting: `arch` under `[infrastructure.express]`, values `x86_64` (default,
current behaviour) and `arm64`. Confirm the real CFN property name and enum
values first (the API calls it `cpuArchitecture`).

- [ ] `its-a-live/loop_stack.py`: read `arch`; set `platform=` on the image
      asset (`ecr_assets.Platform.LINUX_ARM64` vs `LINUX_AMD64`) and the
      architecture property on `CfnExpressGatewayService`. The architecture and
      the image must always change together.
- [ ] `its-a-live/_ecs_express_ops.py`: check nothing there assumes x86
      (it calls `UpdateExpressGatewayService`, so it should be fine).
- [ ] Bump `aws-cdk-lib` minimum in `its-a-live/pyproject.toml` to the version
      that has the property; `uv lock`.
- [ ] Igor, same change (see `igor/AGENTS.md` "Channel config sections"):
  - [ ] `igor/src/igor/integrations/its_a_live.py`: `arch` in `_ECS_EXPRESS_EXTRA`
        and `generate_toml`
  - [ ] `igor/src/igor/app/routes/channels.py`: model field
  - [ ] `igor/frontend/src/api/types.ts`
  - [ ] `igor/frontend/src/utils/channelConfigLayout.ts`: label + row, express only
  - [ ] `ChannelNew.vue` and `ChannelDetail.vue` (edit form): a Select with
        `FieldHelp`; on edit, warn that changing it rebuilds the image and
        redeploys the stack
  - [ ] `igor/AGENTS.md` config table row
  - [ ] tests in `igor/tests/test_channel_packaging.py` (roundtrip + default)
- [ ] `galvanise.py`: add `arch` to the `[infrastructure.express]` template.
- [ ] Docs: `its-a-live/README.md` and `its-a-live/AGENTS.md` express examples.
- [ ] Real throwaway deploy with `arch = "arm64"`: confirm the task runs
      (`/health` on `serve.py`), measure the cold deploy time against the x86
      numbers (644s with CloudFront and a cold emulated build; 219s without
      CloudFront and the image already in ECR), then tear it down.
- [ ] Existing channels: changing `arch` on a deployed channel changes the image
      tag and replaces the task. Check that a plain `redeploy` handles it.
