# Atlas Engineering Roadmap

**Project:** Atlas Physician Scheduling Platform

**Product Owner:** Ron Turner

**Chief Software Architect / Technical Lead:** ChatGPT

---

# Project Status

Current Phase: Sprint 0 – Foundation

Overall Progress:

- [x] Product Vision
- [x] Functional Specification
- [x] MVP Technical Build Plan
- [x] Repository Created
- [x] GitHub Connected
- [ ] Backend Foundation
- [ ] Frontend Foundation
- [ ] Database Foundation
- [ ] Atlas MVP

---

# Sprint 0 – Foundation

## S0-001 Repository Foundation
Status: Complete

Deliverables

- Repository structure
- GitHub repository
- Git configuration
- VS Code
- Python
- Virtual Environment

---

## S0-002 Development Environment
Status: Complete

Deliverables

- Local development environment
- Python configured
- GitHub connected
- Initial repository commit

---

## S0-003 Backend Foundation
Status: Not Started

Deliverables

- Django project
- Django REST Framework
- Configuration
- Environment variables
- Health endpoint

---

## S0-004 Frontend Foundation
Status: Not Started

Deliverables

- React
- TypeScript
- Vite
- Frontend build
- API connection

---

## S0-005 Infrastructure
Status: Not Started

Deliverables

- Docker
- PostgreSQL
- Redis
- Local development stack

---

## S0-006 Authentication
Status: Not Started

Deliverables

- Custom User model
- Login
- Logout
- Roles
- Permissions

---

# Sprint 1

Core Atlas Data Model

- Organization
- Domain
- Facility
- User
- Shift Template
- Contract
- Schedule Block
- Schedule Version

Shift Builder Requirements (Updated)

Reference: `docs/specifications/shift-builder-requirements.md`

- Shift Builder defines generic recurring shift templates (not assigned users).
- Required template fields:
	- facility, name, start_time, end_time
	- active_days_of_week (Sunday through Saturday)
	- weekend_days (Friday/Saturday/Sunday only)
	- night_shift (boolean)
	- default_staffing_count (default 1)
	- active (boolean)
- Current scope constraints:
	- Do not assign users in Shift Builder.
	- Do not use specific calendar dates in Shift Builder.
	- Do not connect Shift Builder to My Schedule yet.

---

# Sprint 2

Request Management

- User Requests
- Scheduler Requests
- Copy Requests
- Request Calendar

---

# Sprint 3

Build Workspace

- Calendar
- Version Tree
- Penalties
- Statistics
- Comments

---

# Sprint 4

Optimizer

- Penalty Engine
- Background Jobs
- Optimization Versions

---

# Sprint 5

Preview / Publish / Live

- Preview
- Publish
- Live Schedule
- Live Editing

---

# Sprint 6

Reports

- Report Builder
- Saved Reports
- Export

---

# Guiding Principles

1. Atlas provides mechanisms, not policies.
2. Simplicity is preferred over automation.
3. Every feature must solve a real scheduling problem.
4. Every optimization is a new search.
5. Product decisions come before implementation decisions.
6. Architecture is reviewed before code is committed.