// Unified lifecycle phase across all three deploy backends (aws-media,
// ecs-express, local-docker), so the UI shows the same badge colors for
// the same real-world state regardless of which backend a channel uses.
//
// Each backend surfaces its own raw status vocabulary (CloudFormation
// StackStatus, MediaLive Channel.State, ECS statusCode, Docker
// State.Status, ...) -- see igor/AGENTS.md and its-a-live/AGENTS.md for
// the full per-backend enums. This module maps all of them down to one
// small Phase enum, and that Phase to one Tag severity, so
// ChannelList.vue (list endpoint, CloudFormation-only "stack_status") and
// ChannelDetail.vue (live "/status" endpoint) agree on what "green" means.
import type { ChannelListItem, ChannelStatus } from '../api/types'

// 'alive' means the backend's own infra genuinely reports serving AND (if
// checked) HlsPlaybackUrl/DashPlaybackUrl actually returned a manifest.
// 'unreachable' is the "running infra, dead playback" gap this used to
// silently collapse into 'running' -- e.g. a MediaPackage endpoint
// pointed at the wrong channel, an empty ECS target group, or a
// CloudFront distribution still propagating (see its-a-live's
// _reachability.py, which produces the `reachable` field this reads).
export type Phase = 'not-deployed' | 'stopped' | 'alive' | 'unreachable' | 'transitioning' | 'failed' | 'unknown'

const PHASE_SEVERITY: Record<Phase, 'success' | 'danger' | 'warn' | 'info' | 'secondary'> = {
  alive: 'success',
  unreachable: 'danger',
  transitioning: 'info',
  stopped: 'warn',
  failed: 'danger',
  'not-deployed': 'secondary',
  unknown: 'secondary',
}

export function phaseSeverity(phase: Phase) {
  return PHASE_SEVERITY[phase]
}

// User-facing labels only (see PHASE_LABEL) lean into the repo's
// mad-scientist theme (see "Galvanise", ItsAliveBanner.vue's "It's a
// Live!") -- the Phase type/PHASE_SEVERITY keys themselves stay plain and
// technical, same split as ActionDef.key ('create') vs. label
// ('Galvanise') elsewhere.
export const PHASE_LABEL: Record<Phase, string> = {
  alive: "It's Alive!",
  // Infra is up, but the manifest check failed -- reanimated, moving, but
  // not actually alive/responsive.
  unreachable: 'Undead',
  transitioning: 'Reanimating',
  stopped: 'Dormant',
  failed: 'Flatlined',
  // Not yet deployed at all -- parts laid out before the experiment even
  // begins, distinct from 'Dormant' (built, then stopped). "Unassembled"
  // was the other candidate here, but franken-ts already owns "Assemble"
  // for building the .ts itself.
  'not-deployed': 'Dismantled',
  unknown: 'Unidentified',
}

// Infra-wise "up" regardless of whether playback is actually reachable --
// i.e. the set of phases where Stop is meaningful / Start should be
// hidden, as opposed to genuinely stopped/never-deployed. Exported so
// ChannelList.vue/ChannelDetail.vue don't each re-derive this union.
export function isUpButMaybeUnreachable(phase: Phase): boolean {
  return phase === 'alive' || phase === 'unreachable'
}

// Downgrades an 'alive' verdict to 'unreachable' when the manifest check
// (see its-a-live's _reachability.py) came back false. `reachable` is
// `null`/`undefined` when no check was performed or possible (e.g. no
// playback URL known yet) -- treated as "can't say it's unreachable",
// not as a failure.
function withReachability(phase: Phase, reachable?: boolean | null): Phase {
  return phase === 'alive' && reachable === false ? 'unreachable' : phase
}

// CloudFormation StackStatus, as returned for aws-media/ecs-express by
// `channel.py list`/`_stack_status` (null when the stack has never been
// created). Covers the full StackStatus enum via substring matching,
// since it's a closed, well-known set of *_IN_PROGRESS / *_COMPLETE /
// *_FAILED / *ROLLBACK* suffixes.
function cfnStackPhase(stackStatus: string | null): Phase {
  if (!stackStatus || stackStatus === 'DELETE_COMPLETE') return 'not-deployed'
  if (stackStatus.includes('ROLLBACK') || stackStatus.includes('FAILED')) return 'failed'
  if (stackStatus.includes('IN_PROGRESS')) return 'transitioning'
  if (stackStatus.includes('COMPLETE')) return 'alive'
  return 'unknown'
}

// Docker container State.Status, plus the "not created" sentinel this
// repo's local-docker ops use when the container doesn't exist yet.
function localDockerPhase(status: string | null, reachable?: boolean | null): Phase {
  switch (status) {
    case 'running':
      return withReachability('alive', reachable)
    case 'not created':
      return 'not-deployed'
    case 'exited':
      return 'stopped'
    case 'dead':
      return 'failed'
    case 'created':
    case 'paused':
    case 'restarting':
    case 'removing':
      return 'transitioning'
    default:
      return status ? 'unknown' : 'not-deployed'
  }
}

// MediaLive Channel.State, as returned by aws-media's live status() op.
function awsMediaLivePhase(status: string, reachable?: boolean | null): Phase {
  switch (status) {
    case 'RUNNING':
    case 'RECOVERING':
      return withReachability('alive', reachable)
    case 'CREATING':
    case 'STARTING':
    case 'STOPPING':
    case 'DELETING':
    case 'UPDATING':
      return 'transitioning'
    case 'IDLE':
      return 'stopped'
    case 'DELETED':
      return 'not-deployed'
    case 'CREATE_FAILED':
    case 'UPDATE_FAILED':
      return 'failed'
    default:
      return 'unknown'
  }
}

/** Phase for a row from the channel list endpoint. `stack_status` alone
 * only says whether the CloudFormation stack is deployed -- for
 * ecs-express/aws-media it stays COMPLETE (-> cfnStackPhase 'alive')
 * whether or not the service is actually serving (scaled to 0 / IDLE) or
 * reachable. `channel.py list` additionally fetches min_tasks/max_tasks,
 * live_status, and `reachable` once the stack has settled (see
 * channelPhase.ts's callers and its-a-live/channel.py's cmd_list) --
 * prefer that real signal whenever it's present, and only fall back to
 * the stack-only phase when it isn't (stack still transitioning/broken/
 * not deployed, or the live fetch failed). */
export function listItemPhase(
  channel: Pick<ChannelListItem, 'backend' | 'stack_status' | 'live_status' | 'min_tasks' | 'reachable'>,
): Phase {
  if (channel.backend === 'local-docker') return localDockerPhase(channel.stack_status, channel.reachable)

  const stackPhase = cfnStackPhase(channel.stack_status)
  if (stackPhase !== 'alive') return stackPhase

  if (channel.backend === 'ecs-express' && channel.min_tasks != null) {
    return channel.min_tasks === 0 ? 'stopped' : withReachability('alive', channel.reachable)
  }
  if (channel.backend === 'aws-media' && channel.live_status) {
    return awsMediaLivePhase(channel.live_status, channel.reachable)
  }
  return stackPhase
}

/** Phase for a channel's live `/status` response. */
export function liveStatusPhase(status: ChannelStatus): Phase {
  if (status.backend === 'aws-media') return awsMediaLivePhase(status.status, status.reachable)
  if (status.backend === 'local-docker') return localDockerPhase(status.status, status.reachable)
  // ecs-express: the statusCode enum doesn't map cleanly to running/stopped;
  // desired task count is the reliable signal (0 == scaled down/stopped).
  if (status.min_tasks === 0) return 'stopped'
  if ((status.min_tasks ?? 0) > 0) return withReachability('alive', status.reachable)
  return 'unknown'
}
