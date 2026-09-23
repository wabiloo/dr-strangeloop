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
export type Phase =
  | 'not-deployed'
  | 'stopped'
  | 'alive'
  | 'unreachable'
  | 'transitioning'
  | 'stopping'
  | 'deleting'
  | 'failed'
  | 'unknown'

const PHASE_SEVERITY: Record<Phase, 'success' | 'danger' | 'warn' | 'info' | 'secondary'> = {
  alive: 'success',
  unreachable: 'danger',
  transitioning: 'info',
  stopping: 'info',
  deleting: 'warn',
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
  // The inverse of 'transitioning'/Reanimating -- infra winding the
  // experiment down toward Dormant rather than up toward alive (MediaLive
  // STOPPING, ECS DRAINING). Distinct from 'deleting'/Dismantling, which
  // tears the stack itself down rather than just pausing it.
  stopping: 'Sedating',
  // Being torn down specifically -- distinct from 'transitioning' so the
  // label doesn't imply coming to life while the stack is actually being
  // deleted (see cfnStackPhase/awsMediaLivePhase).
  deleting: 'Dismantling',
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
// Includes 'stopping' -- infra is still winding down, so Stop is a no-op
// (already in progress) but Start/Terminate are just as premature as they
// are for 'alive'/'unreachable'.
export function isUpButMaybeUnreachable(phase: Phase): boolean {
  return phase === 'alive' || phase === 'unreachable' || phase === 'stopping'
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
  if (stackStatus.startsWith('DELETE_') && stackStatus.includes('IN_PROGRESS')) return 'deleting'
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
      return 'transitioning'
    case 'removing':
      return 'deleting'
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
    case 'UPDATING':
      return 'transitioning'
    case 'STOPPING':
      return 'stopping'
    case 'DELETING':
      return 'deleting'
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

// ECS service status (Express Gateway), as returned by ecs-express's live
// status() op (`service["status"]["statusCode"]`) -- e.g. "ACTIVE",
// "DRAINING" (service scaled down but tasks still terminating -- not
// fully stopped yet, despite minTaskCount already reading 0), "INACTIVE".
// `minTasks` alone can't tell "settled at 0" apart from "still draining
// down to 0", so DRAINING takes priority over the minTasks-based verdict.
function ecsExpressPhase(liveStatus: string | null | undefined, minTasks: number | null | undefined, reachable?: boolean | null): Phase {
  if (liveStatus === 'DRAINING') return 'stopping'
  if (minTasks === 0) return 'stopped'
  if ((minTasks ?? 0) > 0) return withReachability('alive', reachable)
  return 'unknown'
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
    return ecsExpressPhase(channel.live_status, channel.min_tasks, channel.reachable)
  }
  if (channel.backend === 'aws-media' && channel.live_status) {
    return awsMediaLivePhase(channel.live_status, channel.reachable)
  }
  return stackPhase
}

/** Phase for a channel's live `/status` response. */
export function liveStatusPhase(status: ChannelStatus): Phase {
  // Check the CFN stack status first, same as listItemPhase -- while the
  // stack is mid create/update/delete/broken (or gone), `status.status`
  // is only a CFN-status fallback string (see its-a-live's cmd_status),
  // not a real MediaLive/ECS status worth feeding to the backend-specific
  // mapping below.
  if (status.stack_status != null) {
    const stackPhase = cfnStackPhase(status.stack_status)
    if (stackPhase !== 'alive') return stackPhase
  }
  if (status.backend === 'aws-media') return awsMediaLivePhase(status.status, status.reachable)
  if (status.backend === 'local-docker') return localDockerPhase(status.status, status.reachable)
  // ecs-express: `status.status` here IS the raw statusCode (see
  // ecsExpressPhase) -- desired task count is otherwise the reliable
  // running/stopped signal, but DRAINING overrides it.
  return ecsExpressPhase(status.status, status.min_tasks, status.reachable)
}
