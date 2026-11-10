# Conformance capture proxy: static binary on distroless nonroot (spec 15.2).
FROM --platform=$BUILDPLATFORM golang:1.24.7-bookworm@sha256:b8bae5bd9ba9b1f89b635c91c24cc75cea335a16fb5076310f38566fc674b1ec AS build
ARG TARGETOS
ARG TARGETARCH
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
# Cross-compile on the builder's native CPU; the vLLM sidecar runs on ARM64.
RUN GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags="-s -w" -o /out/capture ./cmd/capture

FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=build /out/capture /capture
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/capture"]
