"use client";

import type { WorkflowInputSpec } from "@workspace/api-client";
import { TriangleAlert } from "lucide-react";
import { useId, useState } from "react";
import {
  choiceOptions,
  draftFromInputs,
  inputLabel,
  readInputForm,
  type InputDraft,
  type InputProblem,
} from "./workflow-format";

/**
 * The declared inputs of a workflow, as the form that starts a run.
 *
 * A graph that declares `{{ input.repo }}` is a graph with a parameter, and
 * until this existed the only way to supply one was to POST the payload by
 * hand — so a parameterised workflow was, from the UI, a workflow that could
 * only ever run on its defaults.
 *
 * The submit button is not in here. It is the "Run now" button in the workflow
 * header, tied to this form by `form={formId}`, so a run starts from the same
 * place whether or not the graph takes parameters. A form is rendered even when
 * nothing is declared, for that reason: one code path, one button, one place.
 *
 * Errors are attached to fields rather than collected into a banner, because
 * "the run was refused" is not actionable and "Repository: this one is
 * required" is. The banner exists too, as the `role="alert"` that tells a
 * screen reader something happened at all, and it names the fields.
 */
export type WorkflowInputFormProps = {
  formId: string;
  specs: WorkflowInputSpec[];
  /** The API's own refusal, already attributed to fields by the caller. */
  remoteProblems: InputProblem[];
  /** Called with the typed payload once the form can produce one. */
  onRun: (payload: Record<string, unknown>) => void;
  disabled?: boolean;
};

export function WorkflowInputForm({
  formId,
  specs,
  remoteProblems,
  onRun,
  disabled = false,
}: WorkflowInputFormProps) {
  const [draft, setDraft] = useState<InputDraft>(() => draftFromInputs(specs));
  const [localProblems, setLocalProblems] = useState<InputProblem[]>([]);
  const fieldPrefix = useId();

  // What the browser found beats what the server last said: the server's
  // complaint is about the payload that was sent, and this one has been edited.
  const problems = localProblems.length > 0 ? localProblems : remoteProblems;
  const problemFor = (name: string) =>
    problems.filter((problem) => problem.input === name);
  const formProblems = problems.filter((problem) => problem.input === "");

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (disabled) return;
    const { payload, problems: found } = readInputForm(specs, draft);
    setLocalProblems(found);
    if (found.length === 0) onRun(payload);
  }

  return (
    <form
      id={formId}
      className={specs.length > 0 ? "workflow-inputs" : "workflow-inputs bare"}
      onSubmit={submit}
      noValidate
    >
      {specs.length > 0 && (
        <div className="workflow-inputs-head">
          <strong>Inputs</strong>
          <span>
            This workflow takes parameters. A scheduled run uses the defaults;
            this is where a manual one gets its own.
          </span>
        </div>
      )}

      {problems.length > 0 && (
        <p className="workflow-inputs-alert" role="alert">
          <TriangleAlert size={13} />
          <span>
            {problems.length === 1 ? "One input needs" : `${problems.length} inputs need`}{" "}
            attention:{" "}
            {problems
              .map((problem) =>
                problem.input
                  ? inputLabel(
                      specs.find((spec) => spec.name === problem.input) ?? {
                        ...FALLBACK_SPEC,
                        name: problem.input,
                      },
                    )
                  : "the form",
              )
              .join(", ")}
            .
          </span>
        </p>
      )}

      {specs.map((spec) => {
        const fieldId = `${fieldPrefix}-${spec.name}`;
        const hintId = `${fieldId}-hint`;
        const errorId = `${fieldId}-error`;
        const errors = problemFor(spec.name);
        const described =
          [spec.description ? hintId : "", errors.length > 0 ? errorId : ""]
            .filter(Boolean)
            .join(" ") || undefined;
        const invalid = errors.length > 0;
        const value = draft[spec.name];
        const set = (next: string | boolean) =>
          setDraft((current) => ({ ...current, [spec.name]: next }));

        return (
          <div className="workflow-input" key={spec.name}>
            {spec.type === "boolean" ? (
              <label className="workflow-input-check" htmlFor={fieldId}>
                <input
                  id={fieldId}
                  type="checkbox"
                  checked={value === true}
                  aria-describedby={described}
                  onChange={(event) => set(event.target.checked)}
                />
                <span>{inputLabel(spec)}</span>
              </label>
            ) : (
              <>
                <label htmlFor={fieldId}>
                  {inputLabel(spec)}
                  {!spec.required && <span className="workflow-input-optional">optional</span>}
                </label>
                {spec.choices.length > 0 ? (
                  <select
                    id={fieldId}
                    value={typeof value === "string" ? value : ""}
                    aria-describedby={described}
                    aria-invalid={invalid || undefined}
                    onChange={(event) => set(event.target.value)}
                  >
                    {/* Only when it may legitimately be left unset: a required
                        select that offers "not set" offers a refusal. */}
                    {!spec.required && <option value="">Not set</option>}
                    {choiceOptions(spec).map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                ) : spec.type === "object" || spec.type === "array" ? (
                  <textarea
                    id={fieldId}
                    className="workflow-input-json"
                    rows={3}
                    spellCheck={false}
                    value={typeof value === "string" ? value : ""}
                    aria-describedby={described}
                    aria-invalid={invalid || undefined}
                    onChange={(event) => set(event.target.value)}
                  />
                ) : (
                  <input
                    id={fieldId}
                    type="text"
                    // Deliberately not `type="number"`: a number field silently
                    // discards what it cannot parse, so the box would empty
                    // itself and the message would read "required" about
                    // something that was typed. Our own check says what is
                    // wrong with what is still on screen.
                    inputMode={
                      spec.type === "integer"
                        ? "numeric"
                        : spec.type === "number"
                          ? "decimal"
                          : undefined
                    }
                    value={typeof value === "string" ? value : ""}
                    aria-describedby={described}
                    aria-invalid={invalid || undefined}
                    onChange={(event) => set(event.target.value)}
                  />
                )}
              </>
            )}
            {spec.description && (
              <p className="workflow-input-hint" id={hintId}>
                {spec.description}
              </p>
            )}
            {errors.length > 0 && (
              <p className="workflow-input-error" id={errorId}>
                {errors.map((problem) => problem.message).join(" ")}
              </p>
            )}
          </div>
        );
      })}

      {formProblems.length > 0 && (
        <p className="workflow-input-error">
          {formProblems.map((problem) => problem.message).join(" ")}
        </p>
      )}
    </form>
  );
}

/** Used only to label a problem naming an input the declaration no longer has. */
const FALLBACK_SPEC: WorkflowInputSpec = {
  name: "",
  type: "string",
  label: "",
  description: "",
  required: false,
  default: null,
  choices: [],
};
