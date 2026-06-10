# State Management

## Store Structure
The state must centrally hold:
- `goal`: { startDate, totalDuration, unit }
- `subtasks`: Array of { id, title, weight }

## Data Flow
- User mutations update the raw input values (weights, start date).
- **Derived State**: Do not store the computed start and end dates of subtasks in the raw state. Instead, calculate these on the fly using selectors or memoized hooks to ensure they are always strictly synchronized with the raw inputs.

## Mutation Rules
- Adding or removing a subtask must automatically trigger a re-computation of the `sum of all weights` in derived state.
