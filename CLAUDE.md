# GoalStack Engineering Onboarding

## 1. Project Overview
GoalStack is a weighted goal planner application. It allows users to define a goal with a start date and total duration (in days or hours), and divide it into subtasks with relative effort weights. The system automatically calculates the time allocation and generates a timeline view with precise start and end dates for all subtasks.

## 2. Tech Stack
- React / Next.js
- TypeScript
- Tailwind CSS
- State Management (Zustand / Redux)

## 3. Dev Commands
- Install dependencies: `npm install`
- Run dev server: `npm run dev`
- Build production bundle: `npm run build`

## 4. Core Logic Summary
Each subtask is assigned an effort weight (total up to 100%).
Allocated duration = (subtask weight / sum of all weights) × goal total duration, rounded to 1 decimal place.

## 5. Key Constraints
- Never modify the core weight-to-duration calculation formula.
- Do not assume timezone contexts; handle all dates carefully to avoid off-by-one-day errors.
- Ensure duration rounding is strictly 1 decimal place.
- Maintain relative pathing for .claude/docs references.

## 6. Additional Documentation
- [Architecture Detailed Overview](file:///.claude/docs/architecture.md)
- [State Management Rules](file:///.claude/docs/state_management.md)
- [Date & Allocation Logic](file:///.claude/docs/date_logic.md)
