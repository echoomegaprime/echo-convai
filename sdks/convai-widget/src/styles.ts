/**
 * styles.ts — CSS-in-JS styles for all widget variants.
 *
 * All styles are scoped within Shadow DOM to prevent leaking
 * into or being affected by the host page's styles.
 */

export interface WidgetTheme {
  primaryColor: string;
  backgroundColor: string;
  textColor: string;
  mutedTextColor: string;
  borderRadius: number;
  fontFamily: string;
}

export const DEFAULT_THEME: WidgetTheme = {
  primaryColor: '#6366F1',
  backgroundColor: '#1F2937',
  textColor: '#F9FAFB',
  mutedTextColor: '#9CA3AF',
  borderRadius: 16,
  fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
};

export function generateStyles(theme: WidgetTheme): string {
  return `
    :host {
      all: initial;
      font-family: ${theme.fontFamily};
      color: ${theme.textColor};
    }

    *, *::before, *::after {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    /* ---- Animations ---- */
    @keyframes ecw-fade-in {
      from { opacity: 0; transform: scale(0.9) translateY(8px); }
      to { opacity: 1; transform: scale(1) translateY(0); }
    }
    @keyframes ecw-fade-out {
      from { opacity: 1; transform: scale(1) translateY(0); }
      to { opacity: 0; transform: scale(0.9) translateY(8px); }
    }
    @keyframes ecw-pulse {
      0% { box-shadow: 0 0 0 0 ${theme.primaryColor}66; }
      70% { box-shadow: 0 0 0 12px ${theme.primaryColor}00; }
      100% { box-shadow: 0 0 0 0 ${theme.primaryColor}00; }
    }
    @keyframes ecw-wave {
      0% { transform: scaleY(0.4); }
      50% { transform: scaleY(1.0); }
      100% { transform: scaleY(0.4); }
    }
    @keyframes ecw-spin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }
    @keyframes ecw-dot-pulse {
      0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
      40% { transform: scale(1); opacity: 1; }
    }

    /* ---- Compact Variant (Circle Button) ---- */
    .ecw-compact {
      position: fixed;
      bottom: 24px;
      right: 24px;
      z-index: 999999;
    }
    .ecw-compact-btn {
      width: 60px;
      height: 60px;
      border-radius: 50%;
      border: none;
      background: ${theme.primaryColor};
      color: #fff;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 4px 20px rgba(0,0,0,0.3);
      transition: transform 150ms ease, box-shadow 150ms ease;
      outline: none;
    }
    .ecw-compact-btn:hover {
      transform: scale(1.05);
      box-shadow: 0 6px 24px rgba(0,0,0,0.4);
    }
    .ecw-compact-btn:active {
      transform: scale(0.95);
    }
    .ecw-compact-btn.listening {
      animation: ecw-pulse 2s ease-out infinite;
    }
    .ecw-compact-btn.speaking .ecw-icon {
      display: none;
    }

    .ecw-wave-container {
      display: none;
      align-items: center;
      justify-content: center;
      gap: 3px;
      height: 24px;
    }
    .ecw-compact-btn.speaking .ecw-wave-container {
      display: flex;
    }
    .ecw-wave-bar {
      width: 3px;
      height: 16px;
      background: #fff;
      border-radius: 2px;
      animation: ecw-wave 0.6s ease-in-out infinite;
      transform-origin: center;
    }
    .ecw-wave-bar:nth-child(1) { animation-delay: 0s; }
    .ecw-wave-bar:nth-child(2) { animation-delay: 0.12s; }
    .ecw-wave-bar:nth-child(3) { animation-delay: 0.24s; }
    .ecw-wave-bar:nth-child(4) { animation-delay: 0.36s; }
    .ecw-wave-bar:nth-child(5) { animation-delay: 0.48s; }

    .ecw-status-badge {
      position: absolute;
      top: -4px;
      right: -4px;
      width: 14px;
      height: 14px;
      border-radius: 50%;
      border: 2px solid ${theme.backgroundColor};
    }
    .ecw-status-badge.connected { background: #10B981; }
    .ecw-status-badge.connecting { background: #F59E0B; animation: ecw-spin 1s linear infinite; }
    .ecw-status-badge.error { background: #EF4444; }

    /* ---- Full Variant (Chat Panel) ---- */
    .ecw-full {
      position: fixed;
      bottom: 24px;
      right: 24px;
      z-index: 999999;
      width: 380px;
      max-height: 600px;
      background: ${theme.backgroundColor};
      border-radius: ${theme.borderRadius}px;
      box-shadow: 0 8px 40px rgba(0,0,0,0.4);
      display: flex;
      flex-direction: column;
      overflow: hidden;
      animation: ecw-fade-in 250ms ease;
    }

    .ecw-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 16px 20px;
      background: ${theme.primaryColor};
      color: #fff;
    }
    .ecw-header-left {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .ecw-avatar {
      width: 36px;
      height: 36px;
      border-radius: 50%;
      background: rgba(255,255,255,0.2);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 18px;
    }
    .ecw-header-title {
      font-size: 15px;
      font-weight: 600;
    }
    .ecw-header-status {
      font-size: 12px;
      opacity: 0.8;
    }
    .ecw-close-btn {
      background: none;
      border: none;
      color: #fff;
      cursor: pointer;
      padding: 4px;
      border-radius: 6px;
      display: flex;
      align-items: center;
      justify-content: center;
      opacity: 0.8;
      transition: opacity 150ms;
    }
    .ecw-close-btn:hover { opacity: 1; }

    /* Transcript area */
    .ecw-transcript {
      flex: 1;
      overflow-y: auto;
      padding: 16px 20px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      min-height: 200px;
      max-height: 360px;
      scrollbar-width: thin;
      scrollbar-color: #374151 transparent;
    }
    .ecw-msg {
      max-width: 85%;
      padding: 10px 14px;
      border-radius: 12px;
      font-size: 14px;
      line-height: 1.5;
      word-wrap: break-word;
    }
    .ecw-msg.user {
      align-self: flex-end;
      background: ${theme.primaryColor};
      color: #fff;
      border-bottom-right-radius: 4px;
    }
    .ecw-msg.agent {
      align-self: flex-start;
      background: #374151;
      color: ${theme.textColor};
      border-bottom-left-radius: 4px;
    }
    .ecw-msg.streaming::after {
      content: '';
      display: inline-block;
      width: 6px;
      height: 14px;
      background: ${theme.mutedTextColor};
      margin-left: 2px;
      animation: ecw-dot-pulse 1.4s ease-in-out infinite;
      vertical-align: text-bottom;
    }
    .ecw-typing-dots {
      display: flex;
      gap: 4px;
      padding: 10px 14px;
      align-self: flex-start;
    }
    .ecw-dot {
      width: 8px;
      height: 8px;
      background: ${theme.mutedTextColor};
      border-radius: 50%;
      animation: ecw-dot-pulse 1.4s ease-in-out infinite;
    }
    .ecw-dot:nth-child(2) { animation-delay: 0.2s; }
    .ecw-dot:nth-child(3) { animation-delay: 0.4s; }

    /* Input bar */
    .ecw-input-bar {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 12px 16px;
      border-top: 1px solid #374151;
      background: #111827;
    }
    .ecw-text-input {
      flex: 1;
      background: #1F2937;
      border: 1px solid #374151;
      border-radius: 8px;
      padding: 8px 12px;
      color: ${theme.textColor};
      font-size: 14px;
      font-family: inherit;
      outline: none;
      transition: border-color 150ms;
    }
    .ecw-text-input:focus {
      border-color: ${theme.primaryColor};
    }
    .ecw-text-input::placeholder {
      color: ${theme.mutedTextColor};
    }
    .ecw-mic-btn {
      width: 40px;
      height: 40px;
      border-radius: 50%;
      border: none;
      background: ${theme.primaryColor};
      color: #fff;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      transition: transform 100ms;
    }
    .ecw-mic-btn:active { transform: scale(0.9); }
    .ecw-mic-btn.muted { background: #4B5563; }
    .ecw-mic-btn.listening { animation: ecw-pulse 2s ease-out infinite; }
    .ecw-send-btn {
      width: 40px;
      height: 40px;
      border-radius: 50%;
      border: none;
      background: ${theme.primaryColor};
      color: #fff;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      transition: transform 100ms, opacity 150ms;
    }
    .ecw-send-btn:disabled { opacity: 0.4; cursor: default; }
    .ecw-send-btn:active:not(:disabled) { transform: scale(0.9); }

    /* ---- Expandable Variant ---- */
    .ecw-expandable {
      position: fixed;
      bottom: 24px;
      right: 24px;
      z-index: 999999;
    }
    .ecw-panel-hidden {
      display: none;
    }
    .ecw-panel-visible {
      display: flex;
    }
    .ecw-expandable .ecw-full {
      position: absolute;
      bottom: 72px;
      right: 0;
    }

    /* ---- Empty state ---- */
    .ecw-empty {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 40px 20px;
      color: ${theme.mutedTextColor};
      text-align: center;
      gap: 12px;
    }
    .ecw-empty-icon {
      font-size: 32px;
      opacity: 0.5;
    }
    .ecw-empty-text {
      font-size: 14px;
    }

    /* ---- Volume meter ---- */
    .ecw-volume-bar {
      height: 3px;
      background: ${theme.primaryColor};
      border-radius: 2px;
      transition: width 100ms ease;
      margin: 0 20px;
    }
  `;
}
