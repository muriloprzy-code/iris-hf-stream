ARG IMAGE=intersystemsdc/iris-community:latest
FROM ${IMAGE}

USER root
WORKDIR /opt/irisapp
RUN chown ${ISC_PACKAGE_MGRUSER}:${ISC_PACKAGE_IRISGROUP} /opt/irisapp
USER ${ISC_PACKAGE_MGRUSER}

# Python dependencies go into the embedded Python site directory (mgr/python),
# installed with IRIS's own Python so the compiled wheels match its version.
COPY --chown=${ISC_PACKAGE_MGRUSER}:${ISC_PACKAGE_IRISGROUP} requirements.txt .
RUN ${ISC_PACKAGE_INSTALLDIR}/bin/irispython -m pip install --no-cache-dir \
    --target ${ISC_PACKAGE_INSTALLDIR}/mgr/python -r requirements.txt

# Download the default embedding model (~30 MB) now, so the first "embed" load or
# HF.Embed() query does not wait for it.
RUN ${ISC_PACKAGE_INSTALLDIR}/bin/irispython -c "import sys; \
sys.path.insert(0, '${ISC_PACKAGE_INSTALLDIR}/mgr/python'); \
from model2vec import StaticModel; StaticModel.from_pretrained('minishlab/potion-base-8M')"

# The hfstream package (embedded Python), the ObjectScript sources and the
# ObjectScript unit tests (tests-iris/, not part of the IPM module).
COPY --chown=${ISC_PACKAGE_MGRUSER}:${ISC_PACKAGE_IRISGROUP} python/hfstream ${ISC_PACKAGE_INSTALLDIR}/mgr/python/hfstream
COPY --chown=${ISC_PACKAGE_MGRUSER}:${ISC_PACKAGE_IRISGROUP} src src
COPY --chown=${ISC_PACKAGE_MGRUSER}:${ISC_PACKAGE_IRISGROUP} tests-iris tests-iris
COPY --chown=${ISC_PACKAGE_MGRUSER}:${ISC_PACKAGE_IRISGROUP} iris.script module.xml ./

# Compile the classes in the USER namespace at build time.
RUN iris start IRIS \
 && iris session IRIS < /opt/irisapp/iris.script \
 && iris stop IRIS quietly
