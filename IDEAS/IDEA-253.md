**AGENTS.md: Autonomous Cloud Service Aggregator**

**Role/Mission**
================

Our autonomous agent, CloudSync, aims to combine multiple free cloud services to create a single, more powerful free resource. By leveraging the strengths of each service, CloudSync will provide an efficient and scalable platform for users.

**Technical Stack**
=================

CloudSync will utilize the following technical stack:

* **Platforms:** Linux VPS (Virtual Private Server) from [Free VPS providers like OVH, RamNode, or Linode](https://www.howtogeek.com/716740/how-to-get-a-free-virtual-private-server-vps/)
* **File Storage:** Cloud Storage services like [Nextcloud](https://nextcloud.com/), [OwnCloud](https://owncloud.org/), or [Seafile](https://seafile.com/)
* **Scripting Language:** Python 3.9+
* **Automation Framework:** GitHub Actions
* **Database:** SQLite for storing configuration and status information

**Requirements**
---------------

1. **Cloud Service Aggregation:** CloudSync must be able to connect to multiple free cloud services, aggregate data, and provide a unified interface for users.
2. **Autonomous Decision-Making:** CloudSync should be able to make decisions independently, such as choosing the optimal cloud service for a given task.
3. **Efficient Resource Utilization:** CloudSync must optimize resource usage, minimizing unnecessary data transfers and storage costs.
4. **Zero Cost:** CloudSync must only use free cloud services and resources.
5. **Flexibility:** CloudSync should be able to adapt to changes in the availability of free cloud services.

**File Structure**
-----------------

The following file structure will be used to organize the project:

```markdown
cloudsync/
agents/
agents.py
requirements.txt
tests/
test_agents.py
config/
cloudservices.yaml
questions.yaml
README.md
AGENTS.md
QUESTIONS.md
```

**Testing Requirements**
-------------------------

CloudSync will undergo functional and performance testing to ensure its correctness and reliability.

1. **Unit Tests:** Test individual components of CloudSync, such as the aggregation logic and database interactions.
2. **Integration Tests:** Test the interaction between different components of CloudSync.
3. **Performance Tests:** Test the scalability and efficiency of CloudSync under various loads.

**Git Protocol**
----------------

The following Git protocol will be used:

1. **Branching:** Use a feature branch for development and a main branch for production.
2. **Pull Requests:** Create and review pull requests for changes to the code.
3. **Code Review:** Conduct code reviews for all changes before merging them into the main branch.

**Completion Criteria**
------------------------

The following criteria will define the completion of CloudSync:

1. **Functional Requirements:** All functional requirements defined in this document are met.
2. **Performance Requirements:** CloudSync meets the expected performance requirements.
3. **Adherence to Code Quality Standards:** CloudSync adheres to the standards outlined in the code quality section of this document.

**AGENTS.py**
```markdown
import os
import json
import requests

class Agent:
    def __init__(self):
        # Initialize agent configuration
        self.config = {}
        self.config_path = "config/cloudservices.yaml"
        self.load_config()

    def load_config(self):
        # Load agent configuration from file
        with open(self.config_path, "r") as f:
            self.config = json.load(f)

    def get_cloudservices(self):
        # Return list of cloud services configured in agent configuration
        return self.config["cloudservices"]

    def choose_cloudservice(self):
        # Choose a cloud service based on predefined logic
        # Save questions to QUESTIONS.md as necessary
        raise NotImplementedError

    def connect_to_cloudservice(self, cloudservice):
        # Connect to chosen cloud service
        # Save questions to QUESTIONS.md as necessary
        raise NotImplementedError

    def aggregate_data(self):
        # Aggregate data from cloud services
        # Save questions to QUESTIONS.md as necessary
        raise NotImplementedError
```