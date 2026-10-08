/**
 * Gather the state of every form control on the page.
 *
 * This file GATHERS ONLY. It makes no decisions, fills nothing, and clicks
 * nothing. Whether the form is safe to submit is decided in Python by
 * `verdict.presubmit_verdict`, over the plain objects returned here.
 *
 * That split is the whole design: the decision becomes a pure function that is
 * unit-testable without a browser, and this file stays a dumb, auditable reader
 * of the DOM.
 */
() => {
  const text = (node) => (node?.textContent || "").replace(/\s+/g, " ").trim();

  /**
   * Find the human-visible label for a control.
   *
   * Tried in order of reliability. ATS forms are wildly inconsistent here, and
   * an unlabeled field is one we must escalate rather than guess at, so it is
   * worth trying hard before giving up.
   */
  const labelFor = (el) => {
    if (el.id) {
      const byFor = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (byFor) return text(byFor);
    }
    const wrapping = el.closest("label");
    if (wrapping) return text(wrapping);

    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const parts = labelledBy
        .split(/\s+/)
        .map((id) => text(document.getElementById(id)))
        .filter(Boolean);
      if (parts.length) return parts.join(" ");
    }

    const aria = el.getAttribute("aria-label");
    if (aria) return aria.trim();

    // Fieldsets: a radio group's real question is its legend, not the option.
    const legend = el.closest("fieldset")?.querySelector("legend");
    if (legend) return text(legend);

    // Last resort: the nearest preceding block of text in the same container.
    const container = el.closest("div, li, td, section");
    if (container) {
      const owned = text(container);
      if (owned && owned.length < 200) return owned;
    }
    return el.getAttribute("placeholder") || el.name || "";
  };

  /** How do we know this control is required? Recorded so holds are explainable. */
  const requiredEvidence = (el, label) => {
    if (el.required || el.getAttribute("aria-required") === "true") return "attribute";
    if (el.closest("[data-required='true'], .required, .is-required")) return "class";
    // An asterisk in the label is the most common convention and frequently the
    // only signal, since many frameworks never set the attribute.
    if (/[*∗]|\(required\)/i.test(label)) return "asterisk";
    return "";
  };

  /**
   * Classify the control. Button-pair yes/no widgets and custom comboboxes are
   * common in ATS forms and look nothing like the native elements they replace.
   */
  const kindOf = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === "textarea") return "textarea";
    if (tag === "select") return "select";
    if (tag === "input") {
      const type = (el.type || "text").toLowerCase();
      if (type === "file") return "file";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "email") return "email";
      if (type === "tel") return "phone";
      if (type === "url") return "url";
      if (type === "date") return "date";
      // A text input wired to a listbox is a combobox, and must be verified by
      // readback rather than trusted after typing.
      if (el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete")) {
        return "combobox";
      }
      return "text";
    }
    if (el.getAttribute("role") === "combobox") return "combobox";
    return "unknown";
  };

  const selector = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
    const parent = el.parentElement;
    if (!parent) return el.tagName.toLowerCase();
    const index = Array.from(parent.children).indexOf(el) + 1;
    return `${el.tagName.toLowerCase()}:nth-child(${index})`;
  };

  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (el.type === "hidden") return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 || box.height > 0;
  };

  const out = [];
  const seenRadioGroups = new Set();

  for (const el of document.querySelectorAll(
    "input, select, textarea, [role='combobox']"
  )) {
    if (!visible(el)) continue;
    if (el.disabled || el.readOnly) continue;

    const kind = kindOf(el);
    const label = labelFor(el);

    // A radio group is one question, not N. Report it once, keyed by name, or a
    // five-option question reads as five separate unfilled fields.
    if (kind === "radio" && el.name) {
      if (seenRadioGroups.has(el.name)) continue;
      seenRadioGroups.add(el.name);
      const group = Array.from(
        document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)
      );
      const chosen = group.find((r) => r.checked);
      const legend = el.closest("fieldset")?.querySelector("legend");
      out.push({
        label: legend ? text(legend) : label,
        kind: "radio",
        selector: `input[type=radio][name="${el.name}"]`,
        required: Boolean(requiredEvidence(el, label)) || group.some((r) => r.required),
        evidence: requiredEvidence(el, label) || "none",
        checked: Boolean(chosen),
        value: chosen ? chosen.value : "",
        selected_label: chosen ? labelFor(chosen) : "",
        options: group.map((r) => ({ value: r.value, label: labelFor(r) })),
        aria_invalid: el.getAttribute("aria-invalid") === "true",
      });
      continue;
    }

    const record = {
      label,
      kind,
      selector: selector(el),
      required: Boolean(requiredEvidence(el, label)),
      evidence: requiredEvidence(el, label) || "none",
      value: el.value || "",
      placeholder: el.getAttribute("placeholder") || "",
      aria_invalid: el.getAttribute("aria-invalid") === "true",
      checked: Boolean(el.checked),
      file_count: el.files ? el.files.length : 0,
      combo_text: kind === "combobox" ? el.value || "" : "",
      selected_label: "",
      selected_index: null,
      selected_disabled: false,
      options: [],
    };

    if (kind === "select") {
      const option = el.options[el.selectedIndex];
      record.selected_index = el.selectedIndex;
      record.selected_label = option ? text(option) || option.text : "";
      record.selected_disabled = Boolean(option && option.disabled);
      record.options = Array.from(el.options).map((o) => ({
        value: o.value,
        label: o.text.trim(),
        disabled: Boolean(o.disabled),
      }));
    }

    out.push(record);
  }

  /** Candidate submit controls, reported but never activated here. */
  const submits = Array.from(
    document.querySelectorAll(
      "button[type=submit], input[type=submit], button[id*='submit' i], button[class*='submit' i]"
    )
  )
    .filter(visible)
    .map((el) => ({
      label: text(el) || el.value || "Submit",
      selector: selector(el),
      disabled: Boolean(el.disabled),
    }));

  return { fields: out, submits, url: location.href, title: document.title };
};
