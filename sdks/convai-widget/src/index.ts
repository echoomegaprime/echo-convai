/**
 * @echo-prime/convai-widget — Embeddable conversational AI widget.
 *
 * Entry point for the IIFE/ESM bundle. Exposes `window.EchoConvAI`
 * for script-tag usage and supports auto-detection of data attributes.
 *
 * Usage (script tag — programmatic):
 *   <script src="https://cdn.echo-ept.com/convai-widget.js"></script>
 *   <script>
 *     EchoConvAI.init({
 *       agentId: 'agent_abc123',
 *       serverUrl: 'wss://echo-convai.echo-op.com',
 *       variant: 'expandable',
 *     });
 *   </script>
 *
 * Usage (script tag — data attributes):
 *   <script src="https://cdn.echo-ept.com/convai-widget.js"
 *           data-echo-agent-id="agent_abc123"></script>
 *
 * Usage (npm):
 *   import { init, ConvAIWidget } from '@echo-prime/convai-widget';
 *   const widget = init({ agentId: 'agent_abc123' });
 */

import { ConvAIWidget, type WidgetConfig, type WidgetVariant, type WidgetStatus } from './widget';
import { type WidgetTheme, DEFAULT_THEME, generateStyles } from './styles';

export { ConvAIWidget, type WidgetConfig, type WidgetVariant, type WidgetStatus };
export { type WidgetTheme, DEFAULT_THEME, generateStyles };

// ---------------------------------------------------------------------------
// Global API
// ---------------------------------------------------------------------------

export interface EchoConvAIGlobal {
  /** Create and mount a widget instance */
  init(config: WidgetConfig): ConvAIWidget;
  /** Destroy all active widget instances */
  destroy(): void;
  /** Get the most recently created widget */
  getWidget(): ConvAIWidget | null;
  /** Reference to the widget class for advanced usage */
  Widget: typeof ConvAIWidget;
  /** Track all mounted instances */
  _instances: ConvAIWidget[];
}

let lastWidget: ConvAIWidget | null = null;

const api: EchoConvAIGlobal = {
  init(config: WidgetConfig): ConvAIWidget {
    const widget = new ConvAIWidget(config);

    if (typeof document !== 'undefined') {
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => widget.mount());
      } else {
        widget.mount();
      }
    }

    api._instances.push(widget);
    lastWidget = widget;
    return widget;
  },

  destroy(): void {
    for (const w of api._instances) {
      w.unmount();
    }
    api._instances = [];
    lastWidget = null;
  },

  getWidget(): ConvAIWidget | null {
    return lastWidget;
  },

  Widget: ConvAIWidget,
  _instances: [],
};

// ---------------------------------------------------------------------------
// Window exposure + auto-init
// ---------------------------------------------------------------------------

if (typeof window !== 'undefined') {
  // Check for queued init calls (async script loaded after init was called)
  const existing = (window as unknown as Record<string, unknown>).EchoConvAI;
  if (existing && typeof existing === 'object' && '_queue' in (existing as Record<string, unknown>)) {
    const queue = (existing as { _queue: WidgetConfig[] })._queue;
    (window as unknown as Record<string, unknown>).EchoConvAI = api;
    for (const cfg of queue) {
      api.init(cfg);
    }
  } else {
    (window as unknown as Record<string, unknown>).EchoConvAI = api;
  }

  // Auto-detect script tags with data-echo-agent-id on DOM ready
  const autoInit = () => {
    const scripts = document.querySelectorAll<HTMLScriptElement>(
      'script[data-echo-agent-id]',
    );
    scripts.forEach((script) => {
      const agentId = script.getAttribute('data-echo-agent-id');
      if (!agentId) return;

      api.init({
        agentId,
        serverUrl: script.getAttribute('data-echo-server') ?? undefined,
        variant: (script.getAttribute('data-echo-variant') as WidgetVariant) ?? undefined,
        title: script.getAttribute('data-echo-title') ?? undefined,
        greeting: script.getAttribute('data-echo-greeting') ?? undefined,
        textOnly: script.getAttribute('data-echo-text-only') === 'true',
      });
    });
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoInit);
  } else {
    // Slight defer to let programmatic init() calls take priority over data-attribute auto-init
    setTimeout(autoInit, 0);
  }
}

export default api;
