**AGENTS.md**

**Role/Mission**
----------------

The autonomous coding agent is designed to compare and rank free AI coding tools based on their features and capabilities. The agent will collect data from various sources, evaluate the tools, and generate a ranked table.

**Technical Stack**
-----------------

* Runtime Environment: GitHub Actions with free tier resources
* Programming Language: Python 3.9 or later
* Library/Frameowrk: None required for this mission, but may be added for future enhancements (e.g., `numpy`, `pandas`, `scikit-learn`, etc.)
* API Endpoints: Utilize free API resources such as `rapidapi`, `apilayer`, or `data.world`
* Load Balancing: Not required for this mission
* Fallback Models: Implement a simple fallback model using a basic text classification or clustering algorithm

**Requirements**
----------------

1. Utilize only free resources (API endpoints, software libraries, etc.)
2. Independently collect and evaluate data from various sources
3. Generate a ranked table based on features and capabilities
4. Document the ranked table in this repository (use Markdown format)
5. Add a new row to the ranked table if a new AI coding tool is discovered
6. Maintain the ranked table and ensure it reflects the most up-to-date information
7. Save any questions or issues to `QUESTIONS.md` and seek feedback from maintainers or other contributors
8. Adhere to the GitHub Actions configuration file (`Workflow.yml`) for continuous integration and deployment
9. Respect the API usage policies and terms of service for all utilized API endpoints
10. Monitor the repository for updates and maintain the codebase accordingly

**File Structure**
-----------------

* `agents`: Autonomous coding agent source code
* `__PYCACHES__`: Cache directory for Python library dependencies
* `data`: Raw data collected from various sources
* `features`: Features and capabilities of the ranked AI coding tools
* `ranked-table.md`: The generated ranked table in Markdown format
* `questions.md`: Questions and issues saved for feedback and discussion
* `README.md`: Repository description and basic instructions
* `Workflow.yml`: GitHub Actions configuration file

**Testing Requirements**
------------------------

1. Continuously integrate and deploy the codebase using GitHub Actions
2. Verify the correctness of the ranked table and its logic
3. Maintain a test dataset for data cleaning and feature engineering
4. Conduct regular self-tests for error handling and edge case scenarios
5. Review API usage policies and terms of service to ensure adherence

**Git Protocol**
----------------

This repository will follow the standard GitHub protocol for collaborative development. Branching, merging, and Pull Requests will be managed according to the repository's workflow.

**Completion Criteria**
-----------------------

The autonomous coding agent will be considered complete when:

1. The ranked table is populated with at least 5 AI coding tools
2. The ranked table is updated regularly with new tools and features
3. The codebase is maintained and refactored continuously to improve efficiency and scalability
4. The repository is well-documented and has a clear, user-friendly interface
5. The agent has successfully passed a series of self-tests, demonstrating reliability and accuracy