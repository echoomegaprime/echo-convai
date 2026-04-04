/**
 * ConversationButton — Ready-made button component for Echo ConvAI.
 *
 * Click to start/stop conversations with visual feedback:
 * - Pulsing animation when listening
 * - Waveform animation when agent is speaking
 * - Customizable colors and size
 *
 * Must be used within a <ConversationProvider>.
 */

import React, { useCallback, useMemo, useRef, useEffect } from 'react';
import { useConversationContext, type SessionOptions } from './ConversationProvider';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ConversationButtonProps {
  /** Agent ID to connect to when clicked */
  agentId: string;
  /** Session options passed to startSession */
  sessionOptions?: SessionOptions;
  /** Button size in pixels (default: 64) */
  size?: number;
  /** Primary color for active state (default: '#6366F1') */
  primaryColor?: string;
  /** Idle color when not connected (default: '#374151') */
  idleColor?: string;
  /** Error color (default: '#EF4444') */
  errorColor?: string;
  /** Custom class name */
  className?: string;
  /** Custom inline styles applied to the outer container */
  style?: React.CSSProperties;
  /** Label text shown below the button */
  label?: string;
  /** Whether to show the status text below the button (default: true) */
  showStatus?: boolean;
  /** Accessible aria-label override */
  ariaLabel?: string;
}

// ---------------------------------------------------------------------------
// Keyframes (injected once)
// ---------------------------------------------------------------------------

let stylesInjected = false;

function injectStyles() {
  if (stylesInjected || typeof document === 'undefined') return;
  stylesInjected = true;

  const style = document.createElement('style');
  style.textContent = `
    @keyframes echoConvaiPulse {
      0% { transform: scale(1); opacity: 0.6; }
      50% { transform: scale(1.25); opacity: 0; }
      100% { transform: scale(1.5); opacity: 0; }
    }
    @keyframes echoConvaiWave {
      0% { transform: scaleY(0.4); }
      50% { transform: scaleY(1); }
      100% { transform: scaleY(0.4); }
    }
    @keyframes echoConvaiFadeIn {
      from { opacity: 0; transform: scale(0.9); }
      to { opacity: 1; transform: scale(1); }
    }
  `;
  document.head.appendChild(style);
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function MicIcon({ color, size }: { color: string; size: number }) {
  const iconSize = size * 0.4;
  return (
    <svg
      width={iconSize}
      height={iconSize}
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <path
        d="M12 1C10.34 1 9 2.34 9 4V12C9 13.66 10.34 15 12 15C13.66 15 15 13.66 15 12V4C15 2.34 13.66 1 12 1Z"
        fill={color}
      />
      <path
        d="M17 12C17 14.76 14.76 17 12 17C9.24 17 7 14.76 7 12H5C5 15.53 7.61 18.43 11 18.92V22H13V18.92C16.39 18.43 19 15.53 19 12H17Z"
        fill={color}
      />
    </svg>
  );
}

function StopIcon({ color, size }: { color: string; size: number }) {
  const iconSize = size * 0.3;
  return (
    <svg
      width={iconSize}
      height={iconSize}
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <rect x="4" y="4" width="16" height="16" rx="2" fill={color} />
    </svg>
  );
}

function WaveformBars({
  color,
  size,
  volume,
}: {
  color: string;
  size: number;
  volume: number;
}) {
  const barCount = 5;
  const barWidth = size * 0.05;
  const maxHeight = size * 0.35;
  const gap = size * 0.03;
  const totalWidth = barCount * barWidth + (barCount - 1) * gap;
  const startX = (size - totalWidth) / 2;
  const centerY = size / 2;

  return (
    <>
      {Array.from({ length: barCount }, (_, i) => {
        const delay = i * 0.12;
        const heightMultiplier = 0.3 + volume * 0.7;
        const barHeight = maxHeight * heightMultiplier;
        return (
          <div
            key={i}
            style={{
              position: 'absolute',
              left: startX + i * (barWidth + gap),
              top: centerY - barHeight / 2,
              width: barWidth,
              height: barHeight,
              backgroundColor: color,
              borderRadius: barWidth / 2,
              animation: `echoConvaiWave 0.6s ease-in-out ${delay}s infinite`,
              transformOrigin: 'center',
            }}
          />
        );
      })}
    </>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function ConversationButton({
  agentId,
  sessionOptions,
  size = 64,
  primaryColor = '#6366F1',
  idleColor = '#374151',
  errorColor = '#EF4444',
  className,
  style,
  label,
  showStatus = true,
  ariaLabel,
}: ConversationButtonProps) {
  const {
    startSession,
    endSession,
    status,
    isSpeaking,
    isListening,
    volume,
  } = useConversationContext();

  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    injectStyles();
  }, []);

  const isActive =
    status === 'connected' ||
    status === 'speaking' ||
    status === 'listening';

  const isConnecting = status === 'connecting';
  const isError = status === 'error';

  const handleClick = useCallback(() => {
    if (isActive || isConnecting) {
      endSession();
    } else {
      startSession(agentId, sessionOptions).catch(() => {
        // Error handled by provider
      });
    }
  }, [isActive, isConnecting, agentId, sessionOptions, startSession, endSession]);

  const currentColor = useMemo(() => {
    if (isError) return errorColor;
    if (isActive) return primaryColor;
    return idleColor;
  }, [isError, isActive, primaryColor, idleColor, errorColor]);

  const statusText = useMemo(() => {
    switch (status) {
      case 'idle':
        return label ?? 'Tap to talk';
      case 'connecting':
        return 'Connecting...';
      case 'listening':
        return 'Listening...';
      case 'speaking':
        return 'Speaking...';
      case 'connected':
        return 'Connected';
      case 'error':
        return 'Error';
      default:
        return '';
    }
  }, [status, label]);

  const computedAriaLabel = ariaLabel ?? (isActive ? 'End conversation' : 'Start conversation');

  return (
    <div
      className={className}
      style={{
        display: 'inline-flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 8,
        ...style,
      }}
    >
      <button
        ref={buttonRef}
        onClick={handleClick}
        disabled={false}
        aria-label={computedAriaLabel}
        style={{
          position: 'relative',
          width: size,
          height: size,
          borderRadius: '50%',
          border: 'none',
          backgroundColor: currentColor,
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          transition: 'background-color 200ms ease, transform 100ms ease',
          outline: 'none',
          padding: 0,
          overflow: 'visible',
          animation: 'echoConvaiFadeIn 200ms ease',
        }}
        onMouseDown={() => {
          if (buttonRef.current) {
            buttonRef.current.style.transform = 'scale(0.95)';
          }
        }}
        onMouseUp={() => {
          if (buttonRef.current) {
            buttonRef.current.style.transform = 'scale(1)';
          }
        }}
        onMouseLeave={() => {
          if (buttonRef.current) {
            buttonRef.current.style.transform = 'scale(1)';
          }
        }}
      >
        {/* Pulse ring when listening */}
        {isListening && (
          <div
            style={{
              position: 'absolute',
              inset: -4,
              borderRadius: '50%',
              border: `2px solid ${primaryColor}`,
              animation: 'echoConvaiPulse 2s ease-out infinite',
              pointerEvents: 'none',
            }}
          />
        )}

        {/* Waveform bars when speaking */}
        {isSpeaking ? (
          <WaveformBars color="#FFFFFF" size={size} volume={volume} />
        ) : isActive ? (
          <StopIcon color="#FFFFFF" size={size} />
        ) : (
          <MicIcon color="#FFFFFF" size={size} />
        )}
      </button>

      {showStatus && (
        <span
          style={{
            fontSize: Math.max(11, size * 0.18),
            color: isError ? errorColor : '#9CA3AF',
            fontFamily:
              '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
            fontWeight: 500,
            textAlign: 'center',
            lineHeight: 1.2,
            userSelect: 'none',
          }}
        >
          {statusText}
        </span>
      )}
    </div>
  );
}
