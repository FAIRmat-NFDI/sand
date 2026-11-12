// The bodies of POST …/feedback. Pure functions: tested with node.

// the reasons the "Wrong" dialog offers, as the API names them
export const WRONG_REASONS = [
  ["column", "Wrong column"],
  ["samples", "Wrong samples"],
  ["value", "Wrong value"],
  ["other", "Something else"],
];

export function pageReportBody({ wrong, expected, userAgent, errors }) {
  return {
    what_wrong: wrong.trim(),
    expected: expected.trim(),
    page: "sheet",
    user_agent: userAgent,
    recent_errors: errors,
  };
}

// `meant`: what the user meant instead, optional
export function editReportBody({ key, reason, meant, userAgent, errors }) {
  return {
    what_wrong: "",
    expected: meant.trim(),
    page: "sheet/edit",
    user_agent: userAgent,
    recent_errors: errors,
    edit: { key, reason },
  };
}
