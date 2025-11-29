server:
	python3 oServer.py

client:
	python3 oClient.py

node:
	python3 oNode.py

clean:
	rm -f *.pyc
	rm -f *.pyo
	rm -f *~
	rm -f \#*
	rm -f .\#*
	rm -f .DS_Store
	rm -f ._*
	rm -rf __pycache__/
	rm -rf *.egg-info
	