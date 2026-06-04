// Event types used across the application
export const EVENT_TYPE = {
  ORDER_SKIPPED: 'ORDER_SKIPPED',
  LLM_ANALYSIS: 'LLM_ANALYSIS',
} as const;

// Order statuses
export const ORDER_STATUS = {
  FILLED: 'FILLED',
} as const;

// Runner / control statuses
export const RUNNER_STATUS = {
  COMPLETED: 'COMPLETED',
  FAILED: 'FAILED',
} as const;

// Promise status
export const PROMISE_STATUS = {
  REJECTED: 'rejected',
} as const;
