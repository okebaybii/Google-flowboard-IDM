# Date & Allocation Logic

## Mathematical Core
For any subtask `i`:
```javascript
const sumOfWeights = subtasks.reduce((sum, task) => sum + task.weight, 0);
const rawDuration = (subtask[i].weight / sumOfWeights) * goal.totalDuration;
const allocatedDuration = Math.round(rawDuration * 10) / 10; // Rounded to 1 decimal
```

## Timeline Sequencing
1. The first subtask starts at `goal.startDate`.
2. The end date of subtask `i` is `startDate + allocatedDuration`.
3. The start date of subtask `i+1` is exactly the end date of subtask `i`.

## Edge Cases
- When adding decimals, rounding errors can compound. Implement a check to ensure the final subtask's end date does not exceed the goal's total duration. Adjust the final subtask's duration if necessary to consume the remainder.
