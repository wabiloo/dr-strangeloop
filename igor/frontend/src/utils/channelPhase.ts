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

export type Phase = 'not-deployed' | 'stopped' | 'running' | 'transitioning' | 'failed' | 'unknown'

const PHASE_SEVERITY: Record<Phase, 'success' | 'danger' | 'warn' | 'info' | 'secondary'> = {
  running: 'success',
  transitioning: 'info',
  stopped: 'warn',
  failed: 'danger',
  'not-deployed': 'secondary',
  unknown: 'secondary',
}

export function phaseSeverity(phase: Phase) {
  return PHASE_SEVERITY[phase]
}

export const PHASE_LABEL: Record<Phase, string> = {
  running: 'running',
  transitioning: 'in progress',
  stopped: 'stopped',
  failed: 'failed',
  'not-deployed': 'not deployed',
  unknown: 'unknown',
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
  if (stackStatus.includes('COMPLETE')) return 'running'
  return 'unknown'
}

// Docker container State.Status, plus the "not created" sentinel this
// repo's local-docker ops use when the container doesn't exist yet.
function localDockerPhase(status: string | null): Phase {
  switch (status) {
    case 'running':
      return 'running'
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
function awsMediaLivePhase(status: string): Phase {
  switch (status) {
    case 'RUNNING':
    case 'RECOVERING':
      return 'running'
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

/** Phase for a row from the channel list endpoint (`stack_status` only). */
export function listItemPhase(channel: Pick<ChannelListItem, 'backend' | 'stack_status'>): Phase {
  if (channel.backend === 'local-docker') return localDockerPhase(channel.stack_status)
  return cfnStackPhase(channel.stack_status)
}

/** Phase for a channel's live `/status` response. */
export function liveStatusPhase(status: ChannelStatus): Phase {
  if (status.backend === 'aws-media') return awsMediaLivePhase(status.status)
  if (status.backend === 'local-docker') return localDockerPhase(status.status)
  // ecs-express: the statusCode enum doesn't map cleanly to running/stopped;
  // desired task count is the reliable signal (0 == scaled down/stopped).
  if (status.min_tasks === 0) return 'stopped'
  if ((status.min_tasks ?? 0) > 0) return 'running'
  return 'unknown'
}
