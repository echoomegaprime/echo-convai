/**
 * useAudio — Audio state hook for Echo ConvAI.
 *
 * Provides microphone volume level, mute state, and mute toggle
 * for building custom audio visualization and controls.
 *
 * Must be used within a <ConversationProvider>.
 */

import { useCallback } from 'react';
import { useConversationContext } from './ConversationProvider';

export interface UseAudioReturn {
  /** Current microphone input volume level normalized to 0-1 */
  volume: number;
  /** Whether the microphone is currently muted */
  isMuted: boolean;
  /** Toggle the microphone mute state */
  toggleMute: () => void;
}

/**
 * Hook for accessing audio-specific state.
 *
 * Use this when you only need audio controls without the full conversation
 * state, reducing unnecessary re-renders.
 *
 * @example
 * ```tsx
 * function VolumeIndicator() {
 *   const { volume, isMuted, toggleMute } = useAudio();
 *
 *   return (
 *     <div>
 *       <div
 *         style={{
 *           width: `${volume * 100}%`,
 *           height: 4,
 *           backgroundColor: isMuted ? '#888' : '#4CAF50',
 *           transition: 'width 100ms ease',
 *         }}
 *       />
 *       <button onClick={toggleMute}>
 *         {isMuted ? 'Unmute' : 'Mute'}
 *       </button>
 *     </div>
 *   );
 * }
 * ```
 */
export function useAudio(): UseAudioReturn {
  const ctx = useConversationContext();

  const toggleMute = useCallback(() => ctx.toggleMute(), [ctx]);

  return {
    volume: ctx.volume,
    isMuted: ctx.isMuted,
    toggleMute,
  };
}
