import type { LLMProviderInfo, LLMProviderName } from "../../api/client";

/**
 * One provider card — clickable tile that shows provider identity +
 * connection status + selection state. Used in the OAuth Providers and
 * API Key Providers groups inside AiProvidersSection.
 *
 * Visual contract (matches the screenshot reference):
 *   - Logo dot + name on the left, status badge below
 *   - Right-edge indicator: filled when this card is the user's
 *     pending selection, hollow when not
 *   - Active border when selected; muted border otherwise
 *   - Disabled visual (lower opacity, no pointer) when the provider
 *     can't be activated yet (CLI missing for OAuth) — clicking still
 *     selects so the panel below can guide the user through setup.
 */

interface ProviderCardProps {
  provider: LLMProviderInfo;
  /** True when this card is the user's pending selection (highlighted). */
  selected: boolean;
  /** True when this card matches the currently-applied config (badge). */
  current: boolean;
  /** Live connection-test state for THIS card, when it's the selected one.
   * Undefined for non-selected cards. Drives the honest "Connected" badge:
   * a saved key only means "key present", NOT "key works" — so we never
   * claim Connected until an actual test ping succeeds. */
  testState?: "untested" | "testing" | "ok" | "fail";
  /** Click handler — flips selection. Always fires; the section above
   * decides whether to render setup guidance vs. test flow. */
  onSelect(name: LLMProviderName): void;
}

const PROVIDER_META: Record<
  LLMProviderName,
  { name: string; tagline: string }
> = {
  claude: { name: "Claude API", tagline: "Anthropic API" },
  gemini:  { name: "Google Gemini API", tagline: "Google API · OAuth" },
  openai: { name: "OpenAI API", tagline: "ChatGPT API" },
};

function statusLabel(p: LLMProviderInfo, testState?: ProviderCardProps["testState"]): string {
  // A successful test ping is the ONLY thing that proves the key works.
  if (testState === "ok") return "Connected";
  if (testState === "testing") return "Testing…";
  if (testState === "fail") return "Invalid key";
  if (p.lastError === "not_authenticated") return "Not signed in";
  if (p.requiresKey && !p.configured) return "API key needed";
  // Key is present but unverified — DON'T claim "Connected". The backend's
  // `configured`/`available` only mean "a key exists", not "the key works".
  if (p.configured) return "Key saved · test it";
  return "Setup needed";
}

function statusKind(
  testState?: ProviderCardProps["testState"],
): "ok" | "warn" | "err" {
  if (testState === "ok") return "ok";
  if (testState === "fail") return "err";
  // Saved-but-unverified and all setup states are "warn" (amber), never the
  // confident green — green is reserved for a passing test.
  return "warn";
}

export function ProviderCard({ provider, selected, current, testState, onSelect }: ProviderCardProps) {
  const meta = PROVIDER_META[provider.name];
  const kind = statusKind(testState);

  return (
    <button
      type="button"
      className={`provider-card${selected ? " provider-card--selected" : ""}${
        kind !== "ok" ? " provider-card--unconfigured" : ""
      }`}
      onClick={() => onSelect(provider.name)}
      aria-pressed={selected}
    >
      <div className="provider-card__head">
        <span className="provider-card__name">{meta.name}</span>
        <span className="provider-card__tagline">{meta.tagline}</span>
      </div>
      <div className="provider-card__foot">
        <span className={`provider-card__status provider-card__status--${kind}`}>
          <span className="provider-card__status-dot" aria-hidden="true">●</span>
          {statusLabel(provider, testState)}
        </span>
        {current && !selected && (
          <span className="provider-card__current-badge">Active</span>
        )}
      </div>
      <span
        className={`provider-card__radio${selected ? " provider-card__radio--on" : ""}`}
        aria-hidden="true"
      />
    </button>
  );
}
