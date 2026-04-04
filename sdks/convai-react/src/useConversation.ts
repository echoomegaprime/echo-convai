/**
 * useConversation — Main hook for Echo ConvAI conversations.
 *
 * Provides a complete interface for managing conversation sessions including
 * starting/ending sessions, sending messages, and tracking all state.
 *
 * Must be used within a <ConversationProvider>.
 */

import { useCallback } from 'react';
import {
  useConversationContext,
  type ConversationStatus,
  type SessionOptions,
  type TranscriptEntry,
} from './ConversationProvider';

export interface UseConversationReturn {
  /** Start a conversation session with the given agent */
  startSession: (agentId: string, options?: SessionOptions) => Promise<void>;
  /** End the current session gracefully */
  endSession: () => void;
  /** Send a text message to the agent */
  sendMessage: (text: string) => void;

  /** Whether the agent is currently speaking */
  isSpeaking: boolean;
  /** Whether the system is listening for user input */
  isListening: boolean;
  /** Whether a WebSocket connection is active */
  isConnected: boolean;

  /** Current user transcript (latest utterance) */
  transcript: string;
  /** Current agent response (latest or streaming) */
  agentResponse: string;
  /** Full conversation history */
  history: TranscriptEntry[];

  /** Current connection/conversation status */
  status: ConversationStatus;

  /** Current mic volume level (0-1) */
  volume: number;
  /** Whether the microphone is muted */
  isMuted: boolean;
  /** Toggle microphone mute state */
  toggleMute: () => void;

  /** Last error message, if any */
  error: string | null;

  /** The server-assigned conversation ID */
  conversationId: string | null;
}

/**
 * Primary hook for interacting with Echo ConvAI.
 *
 * @example
 * ```tsx
 * function MyComponent() {
 *   const {
 *     startSession, endSession, sendMessage,
 *     status, isSpeaking, isListening, transcript, agentResponse,
 *   } = useConversation();
 *
 *   return (
 *     <div>
 *       <button onClick={() => startSession('agent_123')}>Start</button>
 *       <button onClick={endSession}>End</button>
 *       <p>Status: {status}</p>
 *       <p>Agent: {agentResponse}</p>
 *     </div>
 *   );
 * }
 * ```
 */
export function useConversation(): UseConversationReturn {
  const ctx = useConversationContext();

  const startSession = useCallback(
    (agentId: string, options?: SessionOptions) =>
      ctx.startSession(agentId, options),
    [ctx],
  );

  const endSession = useCallback(() => ctx.endSession(), [ctx]);

  const sendMessage = useCallback(
    (text: string) => ctx.sendMessage(text),
    [ctx],
  );

  const toggleMute = useCallback(() => ctx.toggleMute(), [ctx]);

  return {
    startSession,
    endSession,
    sendMessage,
    isSpeaking: ctx.isSpeaking,
    isListening: ctx.isListening,
    isConnected: ctx.isConnected,
    transcript: ctx.transcript,
    agentResponse: ctx.agentResponse,
    history: ctx.history,
    status: ctx.status,
    volume: ctx.volume,
    isMuted: ctx.isMuted,
    toggleMute,
    error: ctx.error,
    conversationId: ctx.conversationId,
  };
}
