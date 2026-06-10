# GoalStack Architecture

## Core Principles
GoalStack operates entirely on the client-side for goal calculations. The architecture is modularized into three primary domains:
1. **Goal Configuration**: Inputting the high-level goal, start date, and total duration.
2. **Subtask Breakdown**: Adding and weighting specific tasks.
3. **Timeline Visualization**: Rendering the computed schedule.

## Component Structure
- Keep components small and focused.
- Presentational components handle the Timeline rendering.
- Container components handle the extraction of data from the store and dispatching actions.
