/**
 * @echo-prime/convai-react
 *
 * React SDK for Echo ConvAI — conversational AI components and hooks.
 *
 * @example
 * ```tsx
 * import {
 *   ConversationProvider,
 *   useConversation,
 *   ConversationButton,
 * } from '@echo-prime/convai-react';
 *
 * function App() {
 *   return (
 *     <ConversationProvider defaultServerUrl="wss://echo-convai.echo-op.com">
 *       <ConversationButton agentId="agent_123" />
 *     </ConversationProvider>
 *   );
 * }
 * ```
 */

// Provider
export {
  ConversationProvider,
  useConversationContext,
  type ConversationProviderProps,
  type ConversationContextValue,
  type ConversationStatus,
  type SessionOptions,
  type TranscriptEntry,
} from './ConversationProvider';

// Hooks
export {
  useConversation,
  type UseConversationReturn,
} from './useConversation';

export {
  useAudio,
  type UseAudioReturn,
} from './useAudio';

// Components
export {
  ConversationButton,
  type ConversationButtonProps,
} from './ConversationButton';
