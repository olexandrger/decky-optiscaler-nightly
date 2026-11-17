import { DialogButton, Focusable, PanelSectionRow } from "@decky/ui";
import { useState } from "react";
import type { CSSProperties, ReactNode } from "react";

/**
 * Pieces of the condensed Quick Access panel: rows of real buttons, and a
 * section header whose explanation stays behind an ⓘ.
 *
 * Steam's focus rules, as TabStrip found them: a bare Focusable nested in
 * another is a focus target with nothing to activate, so rows are DialogButtons
 * in a horizontal Focusable; and Steam's focus class turns a button's text dark
 * while expecting the background to turn light, so a button that sets its own
 * background sets both halves for the focused state.
 */

/** Focus handlers Steam's button forwards to the element it renders. */
function focusHandlers(onFocus: () => void, onBlur: () => void) {
  return { onFocus, onBlur } as Record<string, () => void>;
}

function useFocus() {
  const [focused, setFocused] = useState(false);
  return { focused, handlers: focusHandlers(() => setFocused(true), () => setFocused(false)) };
}

function buttonStyle(focused: boolean, extra?: CSSProperties): CSSProperties {
  return {
    padding: "8px 6px",
    fontSize: "12px",
    lineHeight: 1.15,
    minWidth: 0,
    background: focused ? "rgba(255,255,255,0.95)" : "rgba(255,255,255,0.1)",
    color: focused ? "#1a1c20" : "#ffffff",
    opacity: 1,
    ...extra,
  };
}

export interface RowButton {
  label: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  /** For the harness: data-button="<id>". */
  id: string;
}

function RowButtonItem({ button }: Readonly<{ button: RowButton }>) {
  const { focused, handlers } = useFocus();
  return (
    <DialogButton
      {...({ "data-button": button.id } as Record<string, string>)}
      disabled={button.disabled}
      onClick={button.onClick}
      {...handlers}
      style={buttonStyle(focused, { flex: "1 1 0" })}
    >
      {button.label}
    </DialogButton>
  );
}

/** Buttons side by side, left and right moving between them. */
export function ButtonRow({ buttons }: Readonly<{ buttons: RowButton[] }>) {
  return (
    <PanelSectionRow>
      <Focusable style={{ display: "flex", gap: "6px", padding: "4px 0" }} flow-children="horizontal">
        {buttons.map((button) => (
          <RowButtonItem key={button.id} button={button} />
        ))}
      </Focusable>
    </PanelSectionRow>
  );
}

/** The small ⓘ that shows or hides an explanation. */
function InfoButton({ open, onToggle, id }: Readonly<{ open: boolean; onToggle: () => void; id: string }>) {
  const { focused, handlers } = useFocus();
  return (
    <DialogButton
      {...({ "data-info": id, "data-open": open ? "true" : "false" } as Record<string, string>)}
      onClick={onToggle}
      onOKActionDescription={open ? "Hide" : "Explain"}
      {...handlers}
      style={buttonStyle(focused, {
        flex: "0 0 auto",
        width: "26px",
        height: "26px",
        padding: 0,
        borderRadius: "13px",
        fontSize: "13px",
        fontWeight: 700,
        background: focused ? "rgba(255,255,255,0.95)" : open ? "rgba(26,159,255,0.45)" : "rgba(255,255,255,0.1)",
      })}
    >
      i
    </DialogButton>
  );
}

/** What an ⓘ reveals: muted, small, the explanation and nothing else. */
export function InfoText({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <div data-info-text style={{ fontSize: "12px", lineHeight: 1.4, opacity: 0.8, padding: "2px 0 8px" }}>
      {children}
    </div>
  );
}

/**
 * A row with something on the left (a section title, status pills) and an ⓘ on
 * the right that shows `info` under it. Collapsed by default: the panel is read
 * with a game running behind it.
 */
export function InfoRow({
  id,
  children,
  info,
  title,
}: Readonly<{ id: string; children?: ReactNode; info?: ReactNode; title?: string }>) {
  const [open, setOpen] = useState(false);
  return (
    <PanelSectionRow>
      <Focusable
        style={{ display: "flex", alignItems: "center", gap: "8px", padding: title ? "10px 0 2px" : "2px 0" }}
        flow-children="horizontal"
      >
        <div style={{ flex: "1 1 auto", minWidth: 0 }}>
          {title ? (
            <div
              data-section-title
              style={{ fontSize: "12px", fontWeight: 700, letterSpacing: "0.04em", textTransform: "uppercase",
                opacity: 0.75 }}
            >
              {title}
            </div>
          ) : null}
          {children}
        </div>
        {info ? <InfoButton id={id} open={open} onToggle={() => setOpen((value) => !value)} /> : null}
      </Focusable>
      {open && info ? <InfoText>{info}</InfoText> : null}
    </PanelSectionRow>
  );
}
