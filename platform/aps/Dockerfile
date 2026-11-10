# Agent Policy Service: static binary on distroless nonroot (spec 9.1).
FROM golang:1.24.7-bookworm@sha256:b8bae5bd9ba9b1f89b635c91c24cc75cea335a16fb5076310f38566fc674b1ec AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
ENV CGO_ENABLED=0 GOOS=linux GOTOOLCHAIN=local
RUN go build -trimpath -ldflags="-s -w" -o /out/aps ./cmd/aps

FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=build /out/aps /aps
USER 65532:65532
EXPOSE 9001 9002 4317 9090 8081
ENTRYPOINT ["/aps"]
