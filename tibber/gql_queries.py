"""Gql queries"""

HISTORIC_DATA = """
                {{
                  viewer {{
                    home(id: "{0}") {{
                      {1}(resolution: {2}, last: {3}) {{
                        nodes {{
                          from
                          {4}
                          {1}
                        }}
                      }}
                    }}
                  }}
                }}
          """
LIVE_SUBSCRIBE = """
            subscription{
              liveMeasurement(homeId:"%s"){
                accumulatedConsumption
                accumulatedConsumptionLastHour
                accumulatedCost
                accumulatedProduction
                accumulatedProductionLastHour
                accumulatedReward
                averagePower
                currency
                currentL1
                currentL2
                currentL3
                lastMeterConsumption
                lastMeterProduction
                maxPower
                minPower
                power
                powerFactor
                powerProduction
                powerReactive
                signalStrength
                timestamp
                voltagePhase1
                voltagePhase2
                voltagePhase3
            }
           }
        """
PRICE_INFO = """
        {
          viewer {
            home(id: "%s") {
              currentSubscription {
                priceInfo {
                  current {
                    energy
                    tax
                    total
                    startsAt
                    level
                  }
                  today {
                    total
                    startsAt
                    level
                  }
                  tomorrow {
                    total
                    startsAt
                    level
                  }
                }
              }
            }
          }
        }
        """
UPDATE_CURRENT_PRICE = """
        {
          viewer {
            home(id: "%s") {
              currentSubscription {
                priceInfo {
                  current {
                    energy
                    tax
                    total
                    startsAt
                  }
                }
              }
            }
          }
        }
        """
UPDATE_INFO_PRICE = """
        {
          viewer {
            home(id: "%s") {
              currentSubscription {
                priceInfo {
                  current {
                    energy
                    tax
                    total
                    startsAt
                    level
                  }
                  today {
                    total
                    startsAt
                    level
                  }
                  tomorrow {
                    total
                    startsAt
                    level
                  }
                }
              }
              appNickname
              features {
                realTimeConsumptionEnabled
              }
              currentSubscription {
                status
              }
              address {
                address1
                address2
                address3
                city
                postalCode
                country
                latitude
                longitude
              }
              meteringPointData {
                consumptionEan
                energyTaxType
                estimatedAnnualConsumption
                gridCompany
                productionEan
                vatType
              }
              owner {
                name
                isCompany
                language
                contactInfo {
                  email
                  mobile
                }
              }
              timeZone
              subscriptions {
                id
                status
                validFrom
                validTo
                statusReason
              }
              currentSubscription {
                priceInfo {
                  current {
                    currency
                  }
                }
              }
            }
          }
        }

        """
UPDATE_INFO = """
        {
          viewer {
            home(id: "%s") {
              appNickname
              features {
                  realTimeConsumptionEnabled
                }
              currentSubscription {
                status
              }
              address {
                address1
                address2
                address3
                city
                postalCode
                country
                latitude
                longitude
              }
              meteringPointData {
                consumptionEan
                energyTaxType
                estimatedAnnualConsumption
                gridCompany
                productionEan
                vatType
              }
              owner {
                name
                isCompany
                language
                contactInfo {
                  email
                  mobile
                }
              }
              timeZone
              subscriptions {
                id
                status
                validFrom
                validTo
                statusReason
              }
             currentSubscription {
                    priceInfo {
                      current {
                        currency
                      }
                    }
                  }
                }
              }
            }
        """
PUSH_NOTIFICAION = """
        mutation{{
          sendPushNotification(input: {{
            title: "{}",
            message: "{}",
          }}){{
            successful
            pushedToNumberOfDevices
          }}
        }}
        """
INFO = """
        {
          viewer {
            name
            userId
            homes {
              id
              subscriptions {
                status
              }
            }
          }
        }
        """
HISTORIC_PRICE = """
                {{
                  viewer {{
                    home(id: "{0}") {{
                      currentSubscription {{
                        priceRating {{
                            {1} {{
                              entries {{
                                  time
                                  total
                              }}
                            }}
                         }}
                     }}
                  }}
                  }}
                }}
          """
